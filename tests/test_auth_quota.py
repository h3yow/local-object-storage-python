"""命名空间授权与存储配额：越权可区分、并发写入不突破配额。"""

import threading

from app import errors
from app.service import ObjectService
from app.storage import LocalStorage

from .helpers import b64, sha256, setup_namespace, upload


def test_unauthorized_vs_forbidden(client):
    """无凭证 401 unauthorized；有凭证但权限不足 403 forbidden。"""
    headers = setup_namespace(client)
    client.post("/namespaces/ns/grants",
                json={"token": "reader", "read": True, "write": False})

    # 无凭证
    r = client.get("/namespaces/ns/objects/k")
    assert r.status_code == 401 and r.json()["error"]["reason"] == "unauthorized"
    # 无效凭证
    r = client.get("/namespaces/ns/objects/k", headers={"X-Access-Token": "bad"})
    assert r.status_code == 401 and r.json()["error"]["reason"] == "unauthorized"
    # 只读凭证写操作
    r = client.post("/namespaces/ns/uploads",
                    json={"key": "k", "expected_parts": 1, "final_sha256": sha256(b"a")},
                    headers={"X-Access-Token": "reader"})
    assert r.status_code == 403 and r.json()["error"]["reason"] == "forbidden"
    # 只读凭证读操作允许（对象不存在是另一回事）
    r = client.get("/namespaces/ns/objects/k", headers={"X-Access-Token": "reader"})
    assert r.status_code == 404 and r.json()["error"]["reason"] == "object_not_found"


def test_namespace_isolation(client):
    """token 只在一个命名空间有效，跨命名空间访问被拒绝。"""
    setup_namespace(client, ns="ns1", token="tok1")
    setup_namespace(client, ns="ns2", token="tok2")
    r = client.post("/namespaces/ns2/uploads",
                    json={"key": "k", "expected_parts": 1, "final_sha256": sha256(b"a")},
                    headers={"X-Access-Token": "tok1"})
    assert r.status_code == 401 and r.json()["error"]["reason"] == "unauthorized"


def test_quota_exceeded_distinguishable(client, caplog):
    """超配额请求被拒绝且原因可区分，配额账目不变。"""
    headers = setup_namespace(client, quota=10)
    _, r = upload(client, "ns", "k1", [b"12345"], headers)
    assert r.status_code == 201

    r = client.post("/namespaces/ns/uploads", json={
        "key": "k2", "expected_parts": 1, "final_sha256": sha256(b"x" * 6),
    }, headers=headers)
    sid = r.json()["session_id"]
    r = client.put(f"/uploads/{sid}/parts/1",
                   json={"data_base64": b64(b"x" * 6), "sha256": sha256(b"x" * 6)},
                   headers=headers)
    assert r.status_code == 413
    body = r.json()["error"]
    assert body["reason"] == "quota_exceeded"
    assert body["detail"]["quota_bytes"] == 10
    assert body["detail"]["used_bytes"] == 5
    assert "quota rejected" in caplog.text

    svc = client.app.state.service
    assert svc._namespaces["ns"].used_bytes == 5
    assert svc._inflight["ns"] == 0  # 拒绝的分段不占在途配额


def test_inflight_bytes_count_toward_quota(client):
    """未完成会话的在途分段也占配额，防止并发上传绕过限制。"""
    headers = setup_namespace(client, quota=10)
    r = client.post("/namespaces/ns/uploads", json={
        "key": "a", "expected_parts": 1, "final_sha256": sha256(b"a" * 6),
    }, headers=headers)
    sid_a = r.json()["session_id"]
    assert client.put(f"/uploads/{sid_a}/parts/1",
                      json={"data_base64": b64(b"a" * 6), "sha256": sha256(b"a" * 6)},
                      headers=headers).status_code == 200

    # 在途 6 字节 + 新分段 5 字节 > 配额 10
    r = client.post("/namespaces/ns/uploads", json={
        "key": "b", "expected_parts": 1, "final_sha256": sha256(b"b" * 5),
    }, headers=headers)
    sid_b = r.json()["session_id"]
    r = client.put(f"/uploads/{sid_b}/parts/1",
                   json={"data_base64": b64(b"b" * 5), "sha256": sha256(b"b" * 5)},
                   headers=headers)
    assert r.status_code == 413 and r.json()["error"]["reason"] == "quota_exceeded"

    # 中止第一个会话释放在途配额后，第二个可以上传
    assert client.post(f"/uploads/{sid_a}/abort", headers=headers).status_code == 200
    assert client.put(f"/uploads/{sid_b}/parts/1",
                      json={"data_base64": b64(b"b" * 5), "sha256": sha256(b"b" * 5)},
                      headers=headers).status_code == 200


def test_concurrent_writes_never_exceed_quota(tmp_path, caplog):
    """多线程并发上传：成功提交的总量永不超过配额，其余收到 quota_exceeded。"""
    quota = 100
    svc = ObjectService(LocalStorage(tmp_path / "d"))
    svc.create_namespace("ns", quota_bytes=quota)
    n = 16
    payload = b"p" * 10  # 每个对象 10 字节，配额最多容纳 10 个

    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i: int):
        data = payload
        s = svc.create_upload("ns", f"k{i}", expected_parts=1, final_sha256=sha256(data))
        barrier.wait()
        try:
            svc.upload_part(s.session_id, 1, data, sha256(data))
            svc.complete_upload(s.session_id)
            with lock:
                results.append("ok")
        except errors.QuotaExceededError:
            with lock:
                results.append("quota")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = results.count("ok")
    assert ok == quota // len(payload), f"成功数 {ok} 应恰为配额容量 10"
    assert results.count("quota") == n - ok
    ns = svc._namespaces["ns"]
    assert ns.used_bytes == ok * len(payload) <= quota
    assert svc._inflight["ns"] == 0
    assert "quota rejected" in caplog.text


def test_old_versions_count_toward_quota(client):
    """历史版本保留且计入配额：覆盖不会绕过配额。"""
    headers = setup_namespace(client, quota=10)
    _, r = upload(client, "ns", "k", [b"12345"], headers)
    assert r.status_code == 201
    _, r = upload(client, "ns", "k", [b"67890"], headers)
    assert r.status_code == 201
    svc = client.app.state.service
    assert svc._namespaces["ns"].used_bytes == 10
    # 再覆盖任何内容都会超配额
    r = client.post("/namespaces/ns/uploads", json={
        "key": "k", "expected_parts": 1, "final_sha256": sha256(b"z"),
    }, headers=headers)
    sid = r.json()["session_id"]
    r = client.put(f"/uploads/{sid}/parts/1",
                   json={"data_base64": b64(b"z"), "sha256": sha256(b"z")}, headers=headers)
    assert r.status_code == 413 and r.json()["error"]["reason"] == "quota_exceeded"
