"""测试辅助：通过 HTTP 完成一次完整上传。"""

import base64
import hashlib


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def setup_namespace(client, ns="ns", quota=1 << 20, token="tok"):
    assert client.put(f"/namespaces/{ns}", json={"quota_bytes": quota}).status_code == 201
    assert client.post(f"/namespaces/{ns}/grants",
                       json={"token": token, "read": True, "write": True}).status_code == 201
    return {"X-Access-Token": token}


def upload(client, ns, key, parts: list[bytes], headers, metadata=None):
    """完整走一遍分段上传流程，返回 (session_id, complete_response)。"""
    whole = b"".join(parts)
    r = client.post(f"/namespaces/{ns}/uploads", json={
        "key": key,
        "expected_parts": len(parts),
        "final_sha256": sha256(whole),
        "metadata": metadata or {},
    }, headers=headers)
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    for i, p in enumerate(parts, start=1):
        r = client.put(f"/uploads/{sid}/parts/{i}",
                       json={"data_base64": b64(p), "sha256": sha256(p)}, headers=headers)
        assert r.status_code == 200, r.text
    return sid, client.post(f"/uploads/{sid}/complete", headers=headers)
