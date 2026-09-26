"""对象版本：覆盖产生可回溯版本、并发提交确定胜负、删除留痕。"""

import base64
import threading

from app.service import ObjectService
from app.storage import LocalStorage
from app import errors

from .helpers import b64, sha256, setup_namespace, upload


def _body(resp) -> bytes:
    return base64.b64decode(resp.json()["data_base64"])


def test_overwrite_creates_traceable_versions(client, caplog):
    """同名覆盖产生新版本，旧版本可按版本号读取，元数据随版本保存。"""
    headers = setup_namespace(client)
    _, r1 = upload(client, "ns", "k", [b"v1-content"], headers, metadata={"rev": "1"})
    assert r1.status_code == 201 and r1.json()["version"] == 1
    _, r2 = upload(client, "ns", "k", [b"v2-content"], headers, metadata={"rev": "2"})
    assert r2.status_code == 201 and r2.json()["version"] == 2

    # 默认读最新版本
    assert _body(client.get("/namespaces/ns/objects/k", headers=headers)) == b"v2-content"
    # 旧版本可回溯
    r = client.get("/namespaces/ns/objects/k", params={"version": 1}, headers=headers)
    assert _body(r) == b"v1-content"
    assert r.json()["metadata"] == {"rev": "1"}

    # 版本列表完整
    r = client.get("/namespaces/ns/objects/k/versions", headers=headers)
    assert [v["version"] for v in r.json()["versions"]] == [1, 2]

    # 不存在的版本：可区分错误
    r = client.get("/namespaces/ns/objects/k", params={"version": 9}, headers=headers)
    assert r.status_code == 404
    assert r.json()["error"]["reason"] == "version_not_found"
    assert r.json()["error"]["detail"]["available_versions"] == [1, 2]
    assert "version=2" in caplog.text


def test_concurrent_commits_deterministic_winner(tmp_path, caplog):
    """多线程并发提交同一对象：恰好一个成功，其余收到 commit_conflict，
    且失败方能从错误详情中看到获胜版本。"""
    svc = ObjectService(LocalStorage(tmp_path / "d"))
    svc.create_namespace("ns", quota_bytes=1 << 20)
    n = 8
    sessions = []
    for i in range(n):
        data = f"content-{i}".encode()
        s = svc.create_upload("ns", "k", expected_parts=1, final_sha256=sha256(data))
        svc.upload_part(s.session_id, 1, data, sha256(data))
        sessions.append((s, data))

    results: dict[str, object] = {}
    barrier = threading.Barrier(n)

    def commit(s, data):
        barrier.wait()
        try:
            results[s.session_id] = ("ok", svc.complete_upload(s.session_id))
        except errors.CommitConflictError as e:
            results[s.session_id] = ("conflict", e)

    threads = [threading.Thread(target=commit, args=sd) for sd in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results.values() if r[0] == "ok"]
    losers = [r for r in results.values() if r[0] == "conflict"]
    assert len(winners) == 1, f"必须恰好一个胜者: {results}"
    assert len(losers) == n - 1

    win_version = winners[0][1]
    assert win_version.version == 1
    # 失败者的错误详情指向获胜版本，旧版本不会无解释消失
    for _, err in losers:
        assert err.detail["current_version"] == 1
        assert err.detail["base_version"] is None
    # 最终对象内容与胜者一致
    winner_data = dict((s.session_id, d) for s, d in sessions)[win_version.session_id]
    assert svc.get_object("ns", "k")[1] == winner_data
    # 版本列表只有一个版本，没有部分写入
    assert [v.version for v in svc.list_versions("ns", "k")] == [1]
    assert "commit_conflict" in caplog.text and "commit success" in caplog.text


def test_sequential_overwrite_no_conflict(client):
    """串行覆盖不冲突：每个会话看到的 base_version 都是最新的。"""
    headers = setup_namespace(client)
    for i in range(1, 4):
        _, r = upload(client, "ns", "k", [f"v{i}".encode()], headers)
        assert r.status_code == 201 and r.json()["version"] == i


def test_delete_leaves_tombstone_and_history(client, caplog):
    """删除产生 tombstone 版本：最新读取 404，历史版本仍可回溯，删除本身留痕。"""
    headers = setup_namespace(client)
    upload(client, "ns", "k", [b"v1"], headers)
    upload(client, "ns", "k", [b"v2"], headers)

    r = client.delete("/namespaces/ns/objects/k", headers=headers)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["version"] == 3

    r = client.get("/namespaces/ns/objects/k", headers=headers)
    assert r.status_code == 404
    assert r.json()["error"]["reason"] == "object_not_found"

    # 历史版本仍可读取
    assert _body(client.get("/namespaces/ns/objects/k", params={"version": 2},
                            headers=headers)) == b"v2"
    # 版本列表含 tombstone，可追溯
    r = client.get("/namespaces/ns/objects/k/versions", headers=headers)
    versions = r.json()["versions"]
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert versions[-1]["deleted"] is True

    # 重复删除：可区分错误
    r = client.delete("/namespaces/ns/objects/k", headers=headers)
    assert r.status_code == 404
    assert r.json()["error"]["reason"] == "object_not_found"
    assert "tombstone_version=3" in caplog.text


def test_reupload_after_delete(client):
    """删除后可重新上传，版本号继续递增，历史不丢。"""
    headers = setup_namespace(client)
    upload(client, "ns", "k", [b"v1"], headers)
    client.delete("/namespaces/ns/objects/k", headers=headers)
    _, r = upload(client, "ns", "k", [b"v3"], headers)
    assert r.status_code == 201 and r.json()["version"] == 3
    assert _body(client.get("/namespaces/ns/objects/k", headers=headers)) == b"v3"
    assert _body(client.get("/namespaces/ns/objects/k", params={"version": 1},
                            headers=headers)) == b"v1"
