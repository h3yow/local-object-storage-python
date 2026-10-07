"""按版本恢复测试。

断言：恢复产生新的当前版本（内容/类型/元信息与源版本一致）；版本号继续
递增、历史不丢；已删除对象可恢复且结果可区分；目标版本/对象不存在与
对象已删除三种情况可区分；并发恢复/删除/上传结果确定且失败可解释。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import (
    ObjectNotFound,
    QuotaExceeded,
    VersionConflict,
    VersionNotFound,
)
from app.models import RestoreRequest, SessionCreate
from utils import sha256

log = logging.getLogger("object_storage.tests")


def _put(service, object_name: str, data: bytes, *, content_type="text/plain",
         metadata=None) -> int:
    body = SessionCreate(object_name=object_name, total_parts=1,
                         final_checksum=sha256(data), content_type=content_type,
                         metadata=metadata or {})
    s = service.create_session("team-a", "token-a", body)
    service.upload_part("team-a", s.session_id, 1, data, sha256(data))
    return service.complete_session("team-a", s.session_id).version


def _restore(service, object_name: str, version: int, **kw):
    return service.restore_object(
        "team-a", object_name, RestoreRequest(version=version, **kw))


def test_restore_old_version_creates_new_current(
        service, make_namespace, caplog_pretty):
    make_namespace()
    _put(service, "doc.txt", b"alpha", content_type="text/plain",
         metadata={"rev": "1"})
    _put(service, "doc.txt", b"beta-beta", content_type="application/json",
         metadata={"rev": "2"})
    _put(service, "doc.txt", b"gamma-gamma-gamma")

    r = _restore(service, "doc.txt", 1)
    log.info("恢复判定=%s", r.decision)
    assert r.version == 4 and r.restored_from == 1
    assert r.outcome == "restored" and r.was_delete_marked is False
    assert r.checksum == sha256(b"alpha")

    # 历史一条不丢，版本号在链上递增
    info = service.stat_object("team-a", "doc.txt")
    assert [v.version for v in info.versions] == [1, 2, 3, 4]
    assert info.current_version == 4 and info.delete_marker is False
    v4 = info.versions[-1]
    # 内容/类型/元信息与源版本完全一致
    assert v4.content_type == "text/plain" and v4.metadata == {"rev": "1"}
    assert v4.restored_from == 1
    # 默认下载到恢复后的内容
    _, path, cur = service.get_object("team-a", "doc.txt")
    assert cur == 4 and path.read_bytes() == b"alpha"
    # 源历史版本仍然可读
    _, path1, _ = service.get_object("team-a", "doc.txt", version=1)
    assert path1.read_bytes() == b"alpha"


def test_restore_deleted_object_clears_delete_marker(
        service, make_namespace, caplog_pretty):
    make_namespace()
    _put(service, "trash.bin", b"first")
    _put(service, "trash.bin", b"second")
    service.delete_object("team-a", "trash.bin")
    with pytest.raises(ObjectNotFound):
        service.get_object("team-a", "trash.bin")

    r = _restore(service, "trash.bin", 1)
    log.info("已删除对象恢复判定=%s", r.decision)
    # "对象已删除"是可区分的成功：outcome/was_delete_marked 明确标注
    assert r.outcome == "restored_from_deleted" and r.was_delete_marked is True
    assert r.version == 3
    info = service.stat_object("team-a", "trash.bin")
    assert info.delete_marker is False
    assert [v.version for v in info.versions] == [1, 2, 3]
    # 恢复后默认下载即为恢复内容
    _, path, cur = service.get_object("team-a", "trash.bin")
    assert cur == 3 and path.read_bytes() == b"first"


def test_restore_error_cases_are_distinguishable(
        service, make_namespace, caplog_pretty):
    make_namespace()
    _put(service, "real.bin", b"data")

    # 对象不存在 -> object_not_found
    with pytest.raises(ObjectNotFound) as e1:
        _restore(service, "ghost.bin", 1)
    assert e1.value.error_code == "object_not_found"
    # 目标版本不存在 -> version_not_found
    with pytest.raises(VersionNotFound) as e2:
        _restore(service, "real.bin", 9)
    assert e2.value.error_code == "version_not_found"
    assert e2.value.details["available"] == [1]
    # 乐观并发：expected_version 不符 -> version_conflict
    with pytest.raises(VersionConflict) as e3:
        _restore(service, "real.bin", 1, expected_version=5)
    assert e3.value.error_code == "version_conflict"
    log.info("恢复错误区分 not_found=%s version=%s conflict=%s",
             e1.value.error_code, e2.value.error_code, e3.value.error_code)
    # 失败未产生任何新版本
    info = service.stat_object("team-a", "real.bin")
    assert [v.version for v in info.versions] == [1]


def test_restore_counts_toward_quota(service, make_namespace, caplog_pretty):
    """恢复产生的新版本计入配额；超额被拒且原因可区分。"""
    make_namespace(quota_bytes=16)
    _put(service, "q.bin", b"123456")       # v1: 6B
    _put(service, "q.bin", b"abcd")          # v2: 4B，used=10
    assert service.get_namespace_info("team-a").used_bytes == 10

    # 恢复 v1(6B)：10+6=16 <= 16，成功
    r = _restore(service, "q.bin", 1)
    assert r.version == 3
    assert service.get_namespace_info("team-a").used_bytes == 16
    # 再恢复 v2(4B)：16+4=20 > 16，拒绝且可区分
    with pytest.raises(QuotaExceeded) as ei:
        _restore(service, "q.bin", 2)
    assert ei.value.error_code == "quota_exceeded"
    assert ei.value.details["projected"] == 20
    log.info("恢复配额判定 拒绝依据=%s", ei.value.details)
    # 清理历史 v1(6B) 后配额释放，恢复 v2(4B) 即可成功
    service.delete_version("team-a", "q.bin", 1)
    assert service.get_namespace_info("team-a").used_bytes == 10
    r = _restore(service, "q.bin", 2)
    assert r.version == 4
    log.info("释放后恢复成功 version=%d used=%d", r.version,
             service.get_namespace_info("team-a").used_bytes)


def test_concurrent_restore_delete_upload_is_deterministic(
        service, make_namespace, caplog_pretty):
    """并发恢复/删除/继续上传：锁串行化，最终状态一致、每条版本可解释。"""
    make_namespace()
    base_a, base_b = b"content-A", b"content-BB"
    _put(service, "race.bin", base_a)
    _put(service, "race.bin", base_b)

    outcomes: list[tuple[str, object]] = []

    def _restore_v1(i):
        try:
            return ("restore", _restore(service, "race.bin", 1))
        except Exception as e:  # noqa: BLE001
            return ("restore_err", e)

    def _upload(i):
        try:
            payload = f"concurrent-{i}".encode()
            return ("upload", _put(service, "race.bin", payload))
        except Exception as e:  # noqa: BLE001
            return ("upload_err", e)

    def _delete(i):
        try:
            return ("delete", service.delete_object("team-a", "race.bin"))
        except Exception as e:  # noqa: BLE001
            return ("delete_err", e)

    tasks = ([(_restore_v1, i) for i in range(4)]
             + [(_upload, i) for i in range(4)]
             + [(_delete, 0), (_delete, 1)])
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda t: t[0](t[1]), tasks))

    kinds = {}
    for kind, payload in results:
        kinds.setdefault(kind, []).append(payload)
    log.info("并发结果分布=%s", {k: len(v) for k, v in kinds.items()})
    # 所有操作都成功（无条件恢复/覆盖/删除在锁内串行，均能完成）
    assert not kinds.get("restore_err") and not kinds.get("upload_err")
    assert not kinds.get("delete_err")
    assert len(kinds["restore"]) == 4 and len(kinds["upload"]) == 4
    assert len(kinds["delete"]) == 2

    # 版本链：1,2 + 8 个新版本，连续无空洞、无丢失
    info = service.stat_object("team-a", "race.bin")
    chain = [v.version for v in info.versions]
    assert chain == list(range(1, 11)), chain
    assert info.current_version == 10
    # 每个恢复产生的新版本内容都等于源版本 v1
    restored_versions = {r.version for r in kinds["restore"]}
    assert restored_versions <= set(chain)
    for v in info.versions:
        if v.version in restored_versions:
            assert v.checksum == sha256(base_a) and v.restored_from == 1
    # 每个还能查到的版本，按版本号读都必须拿到与摘要一致的完整内容
    for v in info.versions:
        vmeta, path, vno = service.get_object("team-a", "race.bin",
                                              version=v.version)
        assert sha256(path.read_bytes()) == vmeta["checksum"]
    # 删除标记取决于锁内时序，但对象元数据始终自洽
    log.info("最终状态 current=v%d delete_marker=%s chain=%s",
             info.current_version, info.delete_marker, chain)


def test_restore_http_endpoint(client, make_namespace):
    """HTTP 层：POST .../restore 与三种情况的错误码区分。"""
    make_namespace()
    h = {"X-Namespace-Token": "token-a"}

    def _upload(name, data):
        sid = client.post("/namespaces/team-a/uploads", headers=h,
                          json={"object_name": name, "total_parts": 1,
                                "final_checksum": sha256(data)}
                          ).json()["session_id"]
        client.put(f"/namespaces/team-a/uploads/{sid}/parts/1",
                   headers={**h, "X-Part-Checksum": sha256(data)}, content=data)
        assert client.post(f"/namespaces/team-a/uploads/{sid}/complete",
                           headers=h).status_code == 201

    _upload("r.txt", b"v1-content")
    _upload("r.txt", b"v2-content-longer")
    client.delete("/namespaces/team-a/objects/r.txt", headers=h)

    # 已删除对象恢复 -> 201 且标注 was_delete_marked
    r = client.post("/namespaces/team-a/objects/r.txt/restore", headers=h,
                    json={"version": 1})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 3 and body["restored_from"] == 1
    assert body["was_delete_marked"] is True
    assert body["outcome"] == "restored_from_deleted"
    # 默认下载即为恢复内容
    g = client.get("/namespaces/team-a/objects/r.txt/content", headers=h)
    assert g.content == b"v1-content" and g.headers["X-Object-Version"] == "3"

    # 版本不存在 -> 404 version_not_found
    r = client.post("/namespaces/team-a/objects/r.txt/restore", headers=h,
                    json={"version": 42})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"
    # 对象不存在 -> 404 object_not_found
    r = client.post("/namespaces/team-a/objects/nope/restore", headers=h,
                    json={"version": 1})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "object_not_found"
    log.info("HTTP 恢复判定 restored_from_deleted/version_not_found/"
             "object_not_found 可区分")
