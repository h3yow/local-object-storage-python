"""HTTP 端到端：真实 REST 接口上的错误码区分与完整续传。"""
from __future__ import annotations

import logging

from utils import sha256

log = logging.getLogger("object_storage.tests")


def test_http_resume_upload_and_versioned_download(client, make_namespace):
    make_namespace(name="ns", quota_bytes=10000, tokens=["tk"])
    h = {"X-Namespace-Token": "tk"}
    p1, p2 = b"hello-", b"http-world"
    full = p1 + p2

    r = client.post("/namespaces/ns/uploads", headers=h,
                    json={"object_name": "k/f.txt", "total_parts": 2,
                          "final_checksum": sha256(full)})
    sid = r.json()["session_id"]
    assert r.status_code == 201

    assert client.put(f"/namespaces/ns/uploads/{sid}/parts/1",
                      headers={**h, "X-Part-Checksum": sha256(p1)},
                      content=p1).status_code == 200
    # 中断后重放同一分段
    replay = client.put(f"/namespaces/ns/uploads/{sid}/parts/1",
                        headers={**h, "X-Part-Checksum": sha256(p1)}, content=p1)
    assert replay.json()["replayed"] is True
    # 乱序
    bad = client.put(f"/namespaces/ns/uploads/{sid}/parts/3",
                     headers={**h, "X-Part-Checksum": sha256(p2)}, content=p2)
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "part_out_of_order"

    assert client.put(f"/namespaces/ns/uploads/{sid}/parts/2",
                      headers={**h, "X-Part-Checksum": sha256(p2)},
                      content=p2).status_code == 200
    done = client.post(f"/namespaces/ns/uploads/{sid}/complete", headers=h)
    assert done.status_code == 201
    assert done.json()["version"] == 1

    g = client.get("/namespaces/ns/objects/k/f.txt/content", headers=h)
    assert g.status_code == 200 and g.content == full
    assert g.headers["X-Object-Checksum"] == sha256(full)


def test_http_error_codes_are_distinguishable(client, make_namespace):
    make_namespace(name="ns1", quota_bytes=5, tokens=["t1"])
    make_namespace(name="ns2", tokens=["t2"])
    h1 = {"X-Namespace-Token": "t1"}
    # 跨命名空间令牌 -> 403
    r = client.post("/namespaces/ns1/uploads",
                    headers={"X-Namespace-Token": "t2"},
                    json={"object_name": "o", "total_parts": 1,
                          "final_checksum": sha256(b"x")})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "namespace_access_denied"

    # 合法上传但超配额 -> 413（在途字节计入配额，分段提交时即拒）
    blob = b"x" * 6
    r = client.post("/namespaces/ns1/uploads", headers=h1,
                    json={"object_name": "big", "total_parts": 1,
                          "final_checksum": sha256(blob)})
    sid = r.json()["session_id"]
    r = client.put(f"/namespaces/ns1/uploads/{sid}/parts/1",
                   headers={**h1, "X-Part-Checksum": sha256(blob)}, content=blob)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "quota_exceeded"
    log.info("HTTP 超配额响应 details=%s", r.json()["error"]["details"])

    # 分段摘要错误 -> 422
    r = client.post("/namespaces/ns1/uploads", headers=h1,
                    json={"object_name": "o2", "total_parts": 1,
                          "final_checksum": sha256(b"x")})
    sid2 = r.json()["session_id"]
    r = client.put(f"/namespaces/ns1/uploads/{sid2}/parts/1",
                   headers={**h1, "X-Part-Checksum": "0" * 64}, content=b"x")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "part_checksum_mismatch"

    # 未知版本 -> 404 version_not_found
    client.post("/namespaces/ns1/uploads", headers=h1, json={
        "object_name": "small", "total_parts": 1, "final_checksum": sha256(b"x")})
    # 使用一个已存在对象需要先在配额内写入：改用 ns2（无限配额）
    r = client.post("/namespaces/ns2/uploads",
                    headers={"X-Namespace-Token": "t2"},
                    json={"object_name": "a", "total_parts": 1,
                          "final_checksum": sha256(b"x")})
    sid3 = r.json()["session_id"]
    client.put(f"/namespaces/ns2/uploads/{sid3}/parts/1",
               headers={"X-Namespace-Token": "t2",
                        "X-Part-Checksum": sha256(b"x")}, content=b"x")
    client.post(f"/namespaces/ns2/uploads/{sid3}/complete",
                headers={"X-Namespace-Token": "t2"})
    r = client.get("/namespaces/ns2/objects/a/content?version=99",
                   headers={"X-Namespace-Token": "t2"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"


def test_http_optimistic_conflict(client, make_namespace):
    make_namespace(name="ns", tokens=["t"])
    h = {"X-Namespace-Token": "t"}

    def _put(name, data, expected_version=None):
        body = {"object_name": name, "total_parts": 1,
                "final_checksum": sha256(data)}
        if expected_version is not None:
            body["expected_version"] = expected_version
        sid = client.post("/namespaces/ns/uploads", headers=h, json=body).json()["session_id"]
        client.put(f"/namespaces/ns/uploads/{sid}/parts/1",
                   headers={**h, "X-Part-Checksum": sha256(data)}, content=data)
        return client.post(f"/namespaces/ns/uploads/{sid}/complete", headers=h)

    assert _put("o", b"a", expected_version=0).status_code == 201
    assert _put("o", b"b", expected_version=1).status_code == 201
    r = _put("o", b"c", expected_version=1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_conflict"
    # 当前内容仍是 b
    g = client.get("/namespaces/ns/objects/o/content", headers=h)
    assert g.content == b"b" and g.headers["X-Object-Version"] == "2"
