"""历史版本保留策略与手动版本清理测试。

断言：超出保留上限的最老版本被自动清理；可手动删除指定历史版本；
当前生效版本不可删；清理后版本链中凡可查到的版本都读得到完整内容；
共享 blob 不被误删；版本号永不重用。
"""
from __future__ import annotations

import logging

import pytest

from app.errors import (
    CannotDeleteCurrentVersion,
    ObjectNotFound,
    VersionNotFound,
)
from app.models import SessionCreate
from utils import sha256

log = logging.getLogger("object_storage.tests")


def _put(service, name: str, data: bytes, *, namespace: str = "team-a",
         content_type: str = "application/octet-stream",
         metadata: dict | None = None):
    body = SessionCreate(object_name=name, total_parts=1,
                         final_checksum=sha256(data),
                         content_type=content_type,
                         metadata=metadata or {})
    s = service.create_session(namespace, "token-a", body)
    service.upload_part(namespace, s.session_id, 1, data, sha256(data))
    return service.complete_session(namespace, s.session_id)


def test_retention_prunes_oldest_beyond_limit(service, make_namespace):
    """max_history_versions=3：第 4 次写入后最老的 v1 被自动清理。"""
    make_namespace(max_history_versions=3)
    payloads = [f"content-v{i}".encode() for i in range(1, 5)]
    for p in payloads:
        r = _put(service, "rot.bin", p)
        log.info("写入完成 version=%d 判定=%s", r.version, r.decision)

    info = service.stat_object("team-a", "rot.bin")
    assert [v.version for v in info.versions] == [2, 3, 4]
    assert info.current_version == 4
    # 被清理的 v1 按版本号读 -> version_not_found（与对象不存在可区分）
    with pytest.raises(VersionNotFound) as ei:
        service.get_object("team-a", "rot.bin", version=1)
    assert ei.value.details["available"] == [2, 3, 4]
    # 保留的版本必须读得到完整内容
    for vno, expect in [(2, payloads[1]), (3, payloads[2]), (4, payloads[3])]:
        _, path, n = service.get_object("team-a", "rot.bin", version=vno)
        assert path.read_bytes() == expect and n == vno
    log.info("保留策略判定 versions=%s v1 已清理, 留存版本内容完整",
             [v.version for v in info.versions])


def test_retention_never_prunes_current_and_never_reuses_version(
        service, make_namespace):
    """limit=1 时只保留当前版本；继续写入版本号在历史之上递增，不重用。"""
    make_namespace(max_history_versions=1)
    for i in range(1, 4):
        _put(service, "one.bin", f"gen-{i}".encode())
    info = service.stat_object("team-a", "one.bin")
    assert [v.version for v in info.versions] == [3]
    assert info.current_version == 3
    # 再写：新版本号为 4，不会重用被清理的 1/2
    r = _put(service, "one.bin", b"gen-4")
    assert r.version == 4
    info = service.stat_object("team-a", "one.bin")
    assert [v.version for v in info.versions] == [4]
    _, path, _ = service.get_object("team-a", "one.bin")
    assert path.read_bytes() == b"gen-4"
    log.info("版本号单调不重用 current=v%s versions=%s",
             info.current_version, [v.version for v in info.versions])


def test_policy_update_applies_to_existing_objects(service, make_namespace):
    """创建时不限保留，之后 PUT policy 收紧到 2：存量对象立即被裁剪。"""
    make_namespace()  # 默认不限
    for i in range(5):
        _put(service, "bulk.bin", f"data-{i}".encode())
    assert len(service.stat_object("team-a", "bulk.bin").versions) == 5

    summary = service.update_policy("team-a", 2)
    log.info("策略更新判定 pruned=%s disk_freed=%d",
             [(p["object"], p["version"]) for p in summary["pruned"]],
             summary["disk_bytes_freed"])
    assert summary["pruned_count"] == 3
    assert summary["disk_bytes_freed"] > 0
    info = service.stat_object("team-a", "bulk.bin")
    assert [v.version for v in info.versions] == [4, 5]
    # 留存版本内容完整
    for vno, expect in [(4, b"data-3"), (5, b"data-4")]:
        _, path, _ = service.get_object("team-a", "bulk.bin", version=vno)
        assert path.read_bytes() == expect
    # 命名空间记录已更新
    assert service.get_namespace_info("team-a").max_history_versions == 2


def test_manual_delete_version_and_current_protected(service, make_namespace):
    """手动删除指定历史版本；当前生效版本不可删，错误码可区分。"""
    make_namespace()
    for i in range(3):
        _put(service, "man.bin", f"m-{i}".encode())

    out = service.delete_version("team-a", "man.bin", 1)
    log.info("手动清理判定=%s", out)
    assert out["deleted_version"] == 1
    assert out["remaining_versions"] == [2, 3]
    with pytest.raises(VersionNotFound):
        service.get_object("team-a", "man.bin", version=1)

    # 当前版本 v3 不允许被直接删
    with pytest.raises(CannotDeleteCurrentVersion) as ei:
        service.delete_version("team-a", "man.bin", 3)
    assert ei.value.error_code == "cannot_delete_current_version"
    assert ei.value.details["current_version"] == 3
    log.info("当前版本删除被拒 依据=%s", ei.value.details)

    # 对象/版本不存在的错误可区分
    with pytest.raises(ObjectNotFound):
        service.delete_version("team-a", "ghost.bin", 1)
    with pytest.raises(VersionNotFound):
        service.delete_version("team-a", "man.bin", 99)

    # 剩余版本读完整内容；版本号继续递增
    _, path, _ = service.get_object("team-a", "man.bin", version=2)
    assert path.read_bytes() == b"m-1"
    r = _put(service, "man.bin", b"m-3")
    assert r.version == 4


def test_prune_shared_blob_not_removed(service, make_namespace):
    """同一 checksum 被其它对象/版本引用时，清理版本不删共享 blob。"""
    make_namespace(max_history_versions=1)
    shared = b"shared-payload"
    _put(service, "a.bin", shared)          # a v1 = shared
    _put(service, "b.bin", shared)          # b v1 = shared（同一 blob）
    _put(service, "a.bin", b"a-second")     # a v2；retention=1 清掉 a v1
    info = service.stat_object("team-a", "a.bin")
    assert [v.version for v in info.versions] == [2]
    # b.bin 仍引用 shared blob，必须可读
    _, path, _ = service.get_object("team-a", "b.bin", version=1)
    assert path.read_bytes() == shared
    # blob 文件仍在
    assert service.store.blob_path("team-a", sha256(shared)).exists()
    log.info("共享 blob 保护判定：a v1 已清理，b v1 内容完整 checksum=%s",
             sha256(shared))


def test_prune_same_object_shared_checksum_keeps_blob(service, make_namespace):
    """同一对象相邻版本内容相同：清理旧版本后新版本仍读得到。"""
    make_namespace(max_history_versions=1)
    same = b"same-content"
    _put(service, "dup.bin", same)          # v1
    _put(service, "dup.bin", same)          # v2 同 checksum；v1 被清理
    info = service.stat_object("team-a", "dup.bin")
    assert [v.version for v in info.versions] == [2]
    _, path, _ = service.get_object("team-a", "dup.bin", version=2)
    assert path.read_bytes() == same
    log.info("同对象共享 checksum：v1 清理后 v2 内容完整")


def test_http_policy_and_version_delete_endpoints(client, make_namespace):
    """HTTP 层：PUT policy 生效；DELETE ?version=N 删历史版本；当前版本 409。"""
    make_namespace(name="ns", tokens=["tk"], max_history_versions=0)
    h = {"X-Namespace-Token": "tk"}

    def put(name, data):
        r = client.post("/namespaces/ns/uploads", headers=h,
                        json={"object_name": name, "total_parts": 1,
                              "final_checksum": sha256(data)})
        sid = r.json()["session_id"]
        client.put(f"/namespaces/ns/uploads/{sid}/parts/1",
                   headers={**h, "X-Part-Checksum": sha256(data)}, content=data)
        return client.post(f"/namespaces/ns/uploads/{sid}/complete",
                           headers=h)

    for i in range(3):
        assert put("h.bin", f"h-{i}".encode()).status_code == 201

    r = client.put("/namespaces/ns/policy", headers=h,
                   json={"max_history_versions": 2})
    assert r.status_code == 200
    assert r.json()["max_history_versions"] == 2
    info = client.get("/namespaces/ns/objects/h.bin", headers=h).json()
    assert [v["version"] for v in info["versions"]] == [2, 3]

    # 手动删 v2
    r = client.delete("/namespaces/ns/objects/h.bin?version=2", headers=h)
    assert r.status_code == 200
    assert r.json()["remaining_versions"] == [3]
    # 当前版本 v3 -> 409 cannot_delete_current_version
    r = client.delete("/namespaces/ns/objects/h.bin?version=3", headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "cannot_delete_current_version"
    # 不存在的版本 -> 404 version_not_found
    r = client.delete("/namespaces/ns/objects/h.bin?version=2", headers=h)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"
    log.info("HTTP 保留策略/版本删除错误码判定通过")
