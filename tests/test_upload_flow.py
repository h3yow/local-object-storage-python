"""分段上传：断点续传、重复分段幂等、摘要错误、非法顺序、会话失效。"""

import logging
import time

from app import errors
from app.service import ObjectService
from app.storage import LocalStorage

from .helpers import b64, sha256, setup_namespace, upload


def test_multipart_upload_and_resume(client, caplog):
    """中断后可续传：已上传的分段保留，只需补传缺失分段。"""
    headers = setup_namespace(client)
    parts = [b"aaa", b"bbb", b"ccc"]
    whole = b"".join(parts)
    r = client.post("/namespaces/ns/uploads", json={
        "key": "k", "expected_parts": 3, "final_sha256": sha256(whole),
    }, headers=headers)
    sid = r.json()["session_id"]

    # 模拟中断：只传了第 1 段
    assert client.put(f"/uploads/{sid}/parts/1",
                      json={"data_base64": b64(parts[0]), "sha256": sha256(parts[0])},
                      headers=headers).status_code == 200
    # 未完成时完成请求被拒绝，且原因可区分
    r = client.post(f"/uploads/{sid}/complete", headers=headers)
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "missing_parts"
    assert r.json()["error"]["detail"]["missing_parts"] == [2, 3]
    # 未完成上传不作为可下载对象出现
    r = client.get("/namespaces/ns/objects/k", headers=headers)
    assert r.status_code == 404
    assert r.json()["error"]["reason"] == "object_not_found"

    # 断点续传：补传剩余分段后完成
    for i in (2, 3):
        assert client.put(f"/uploads/{sid}/parts/{i}",
                          json={"data_base64": b64(parts[i - 1]), "sha256": sha256(parts[i - 1])},
                          headers=headers).status_code == 200
    r = client.post(f"/uploads/{sid}/complete", headers=headers)
    assert r.status_code == 201
    assert r.json()["version"] == 1
    assert r.json()["sha256"] == sha256(whole)

    r = client.get("/namespaces/ns/objects/k", headers=headers)
    assert r.status_code == 200
    import base64
    assert base64.b64decode(r.json()["data_base64"]) == whole

    logs = caplog.text
    assert sid in logs and "version=1" in logs


def test_duplicate_part_is_idempotent(client, caplog):
    """同一分段重复提交（内容一致）幂等成功，只计一次配额。"""
    headers = setup_namespace(client)
    whole = b"hello world"
    r = client.post("/namespaces/ns/uploads", json={
        "key": "k", "expected_parts": 1, "final_sha256": sha256(whole),
    }, headers=headers)
    sid = r.json()["session_id"]
    body = {"data_base64": b64(whole), "sha256": sha256(whole)}
    assert client.put(f"/uploads/{sid}/parts/1", json=body, headers=headers).status_code == 200
    assert client.put(f"/uploads/{sid}/parts/1", json=body, headers=headers).status_code == 200
    assert "idempotent" in caplog.text

    svc = client.app.state.service
    assert svc._inflight["ns"] == len(whole)  # 重复分段未重复计费
    assert client.post(f"/uploads/{sid}/complete", headers=headers).status_code == 201


def test_part_conflict_rejected(client):
    """同一分段号提交不同内容被拒绝，原因可区分。"""
    headers = setup_namespace(client)
    r = client.post("/namespaces/ns/uploads", json={
        "key": "k", "expected_parts": 1, "final_sha256": sha256(b"a"),
    }, headers=headers)
    sid = r.json()["session_id"]
    assert client.put(f"/uploads/{sid}/parts/1",
                      json={"data_base64": b64(b"a"), "sha256": sha256(b"a")},
                      headers=headers).status_code == 200
    r = client.put(f"/uploads/{sid}/parts/1",
                   json={"data_base64": b64(b"b"), "sha256": sha256(b"b")}, headers=headers)
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "part_conflict"
    assert "existing_sha256" in r.json()["error"]["detail"]


def test_part_checksum_mismatch(client, caplog):
    """分段内容与声明摘要不一致被拒绝。"""
    headers = setup_namespace(client)
    r = client.post("/namespaces/ns/uploads", json={
        "key": "k", "expected_parts": 1, "final_sha256": sha256(b"a"),
    }, headers=headers)
    sid = r.json()["session_id"]
    r = client.put(f"/uploads/{sid}/parts/1",
                   json={"data_base64": b64(b"a"), "sha256": sha256(b"tampered")},
                   headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["reason"] == "part_checksum_mismatch"
    assert "part_checksum_mismatch" in caplog.text


def test_final_digest_mismatch(client, caplog):
    """完成时合并内容与声明摘要不一致：拒绝并清理会话，不留悬挂数据。"""
    headers = setup_namespace(client)
    r = client.post("/namespaces/ns/uploads", json={
        "key": "k", "expected_parts": 1, "final_sha256": sha256(b"declared"),
    }, headers=headers)
    sid = r.json()["session_id"]
    assert client.put(f"/uploads/{sid}/parts/1",
                      json={"data_base64": b64(b"actual"), "sha256": sha256(b"actual")},
                      headers=headers).status_code == 200
    r = client.post(f"/uploads/{sid}/complete", headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["reason"] == "digest_mismatch"
    detail = r.json()["error"]["detail"]
    assert detail["actual"] == sha256(b"actual")

    svc = client.app.state.service
    assert svc._inflight["ns"] == 0  # 在途配额已释放
    assert not svc.storage._upload_dir("ns", sid).exists()  # 分段文件已清理
    # 会话已作废，再操作得到可区分错误
    r = client.post(f"/uploads/{sid}/complete", headers=headers)
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "upload_session_invalid_state"
    assert "digest_mismatch" in caplog.text


def test_invalid_part_number(client):
    """分段号超出声明范围被拒绝。"""
    headers = setup_namespace(client)
    r = client.post("/namespaces/ns/uploads", json={
        "key": "k", "expected_parts": 2, "final_sha256": sha256(b"ab"),
    }, headers=headers)
    sid = r.json()["session_id"]
    r = client.put(f"/uploads/{sid}/parts/3",
                   json={"data_base64": b64(b"x"), "sha256": sha256(b"x")}, headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["reason"] == "invalid_part_number"


def test_expired_session_rejected(tmp_path, caplog):
    """过期会话被拒绝并回收，不留下悬挂数据。"""
    svc = ObjectService(LocalStorage(tmp_path / "d"), session_ttl=0.05)
    svc.create_namespace("ns", quota_bytes=100)
    s = svc.create_upload("ns", "k", expected_parts=1, final_sha256=sha256(b"z"))
    svc.upload_part(s.session_id, 1, b"z", sha256(b"z"))
    time.sleep(0.06)

    try:
        svc.upload_part(s.session_id, 1, b"z", sha256(b"z"))
        raise AssertionError("应拒绝过期会话")
    except errors.SessionExpiredError:
        pass
    assert svc._inflight["ns"] == 0
    assert not svc.storage._upload_dir("ns", s.session_id).exists()
    assert "session" in caplog.text and s.session_id in caplog.text


def test_reap_expired_sessions(tmp_path, caplog):
    """回收器清理过期会话并释放配额。"""
    svc = ObjectService(LocalStorage(tmp_path / "d"), session_ttl=0.01)
    svc.create_namespace("ns", quota_bytes=10)
    s = svc.create_upload("ns", "k", expected_parts=1, final_sha256=sha256(b"z"))
    svc.upload_part(s.session_id, 1, b"z", sha256(b"z"))
    assert svc._inflight["ns"] == 1
    time.sleep(0.02)
    reaped = svc.reap_expired_sessions()
    assert reaped == [s.session_id]
    assert svc._inflight["ns"] == 0
    assert not svc.storage._upload_dir("ns", s.session_id).exists()
    assert "session reaped" in caplog.text


def test_aborted_session_rejected(client):
    """中止后的会话不可继续使用。"""
    headers = setup_namespace(client)
    r = client.post("/namespaces/ns/uploads", json={
        "key": "k", "expected_parts": 1, "final_sha256": sha256(b"a"),
    }, headers=headers)
    sid = r.json()["session_id"]
    assert client.post(f"/uploads/{sid}/abort", headers=headers).status_code == 200
    r = client.put(f"/uploads/{sid}/parts/1",
                   json={"data_base64": b64(b"a"), "sha256": sha256(b"a")}, headers=headers)
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "upload_session_invalid_state"


def test_unknown_session(client):
    headers = setup_namespace(client)
    r = client.put("/uploads/nope/parts/1",
                   json={"data_base64": b64(b"a"), "sha256": sha256(b"a")}, headers=headers)
    assert r.status_code == 404
    assert r.json()["error"]["reason"] == "upload_session_not_found"
