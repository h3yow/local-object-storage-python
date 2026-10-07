"""历史版本保留策略与手动清理测试。

断言：保留策略自动裁剪最旧历史；清理后留存版本内容完整可读；
配额同步释放；共享 blob 不误删；版本号永不复用；当前生效版本受保护。
"""
from __future__ import annotations

import logging

import pytest

from app.errors import VersionInUse, VersionNotFound
from app.models import SessionCreate
from utils import sha256

log = logging.getLogger("object_storage.tests")


def _put(service, object_name: str, data: bytes, *, content_type="text/plain",
         metadata=None) -> int:
    """完成一次单分段上传，返回新版本号。"""
    body = SessionCreate(object_name=object_name, total_parts=1,
                         final_checksum=sha256(data), content_type=content_type,
                         metadata=metadata or {})
    s = service.create_session("team-a", "token-a", body)
    service.upload_part("team-a", s.session_id, 1, data, sha256(data))
    return service.complete_session("team-a", s.session_id).version


def test_retention_policy_auto_prunes_oldest_history(
        service, make_namespace, caplog_pretty):
    """max_history_versions=2：只保留当前版本 + 最新 2 个历史版本。"""
    make_namespace(max_history_versions=2)
    contents = {i: bytes([65 + i]) * (10 * (i + 1)) for i in range(1, 6)}
    for i in range(1, 6):
        v = _put(service, "doc.bin", contents[i])
        assert v == i
        log.info("提交 v%d 后 used=%d", i,
                 service.get_namespace_info("team-a").used_bytes)

    info = service.stat_object("team-a", "doc.bin")
    assert [v.version for v in info.versions] == [3, 4, 5]
    assert info.current_version == 5
    # 配额只计留存版本：v3+v4+v5 = 40+50+60
    assert service.get_namespace_info("team-a").used_bytes == 40 + 50 + 60

    # 被清理的版本：元数据与内容都不可取（可区分的 version_not_found）
    for gone in (1, 2):
        with pytest.raises(VersionNotFound):
            service.get_object("team-a", "doc.bin", version=gone)
        assert not service.store.blob_path(
            "team-a", sha256(contents[gone])).exists()
    # 留存版本按版本号读必须拿到完整内容
    for keep in (3, 4, 5):
        vmeta, path, vno = service.get_object("team-a", "doc.bin", version=keep)
        assert path.read_bytes() == contents[keep]
        assert vmeta["checksum"] == sha256(contents[keep])
    log.info("保留策略判定 versions=%s used=%d",
             [v.version for v in info.versions],
             service.get_namespace_info("team-a").used_bytes)


def test_retention_prune_keeps_shared_blob_alive(service, make_namespace):
    """被清理版本的内容若仍被其他对象引用，blob 不得删除。"""
    make_namespace(max_history_versions=0)
    shared = b"shared-content-between-objects"
    _put(service, "x.bin", shared)
    _put(service, "y.bin", shared)  # 同内容，内容寻址共享同一 blob
    # 覆盖 x.bin：v1 立即被策略清理，但 blob 仍被 y.bin 引用
    _put(service, "x.bin", b"x-new-content")

    meta_x = service.stat_object("team-a", "x.bin")
    assert [v.version for v in meta_x.versions] == [2]
    # 共享 blob 仍在，y.bin 可读
    assert service.store.blob_path("team-a", sha256(shared)).exists()
    _, path, _ = service.get_object("team-a", "y.bin")
    assert path.read_bytes() == shared
    # 配额按逻辑引用计：x.v2(13) + y.v1(30)
    used = service.get_namespace_info("team-a").used_bytes
    log.info("共享 blob 保护判定 used=%d (x.v2=%d + y.v1=%d)",
             used, len(b"x-new-content"), len(shared))
    assert used == len(b"x-new-content") + len(shared)


def test_manual_delete_version_rules(service, make_namespace, caplog_pretty):
    """手动清理：历史版本可删；当前生效版本受保护；版本号永不复用。"""
    make_namespace()
    contents = {1: b"v-one", 2: b"v-two!!", 3: b"v-three!!"}
    for i in (1, 2, 3):
        _put(service, "obj.bin", contents[i])
    used0 = service.get_namespace_info("team-a").used_bytes
    assert used0 == sum(len(c) for c in contents.values())

    # 当前生效版本不允许直接删：可区分的 version_in_use (409)
    with pytest.raises(VersionInUse) as ei:
        service.delete_version("team-a", "obj.bin", 3)
    assert ei.value.error_code == "version_in_use"
    log.info("当前版本保护判定=%s", ei.value.details)

    # 不存在的版本：version_not_found (404)，与上面可区分
    with pytest.raises(VersionNotFound):
        service.delete_version("team-a", "obj.bin", 99)

    # 删除历史版本 v2：配额同步释放，blob 回收，v1/v3 不受影响
    r = service.delete_version("team-a", "obj.bin", 2)
    log.info("手动清理判定=%s", r)
    assert r["deleted_version"] == 2 and r["bytes_freed"] == len(contents[2])
    assert r["remaining_versions"] == [1, 3]
    assert service.get_namespace_info("team-a").used_bytes == used0 - len(
        contents[2])
    with pytest.raises(VersionNotFound):
        service.get_object("team-a", "obj.bin", version=2)
    for keep in (1, 3):
        _, path, _ = service.get_object("team-a", "obj.bin", version=keep)
        assert path.read_bytes() == contents[keep]

    # 版本号不重排不复用：下一次提交是 v4，空出的 v2 不会被重新分配
    assert _put(service, "obj.bin", b"v-four") == 4
    info = service.stat_object("team-a", "obj.bin")
    assert [v.version for v in info.versions] == [1, 3, 4]
    log.info("版本号不复用判定 chain=%s",
             [v.version for v in info.versions])


def test_delete_version_http_endpoint(client, make_namespace):
    """HTTP 层：DELETE .../versions/{n} 路由与错误码。"""
    make_namespace()
    h = {"X-Namespace-Token": "token-a"}
    for data in (b"one", b"two"):
        sid = client.post("/namespaces/team-a/uploads", headers=h,
                          json={"object_name": "a/b.txt", "total_parts": 1,
                                "final_checksum": sha256(data)}
                          ).json()["session_id"]
        client.put(f"/namespaces/team-a/uploads/{sid}/parts/1",
                   headers={**h, "X-Part-Checksum": sha256(data)}, content=data)
        assert client.post(
            f"/namespaces/team-a/uploads/{sid}/complete",
            headers=h).status_code == 201

    # 删历史版本 v1 -> 200
    r = client.delete("/namespaces/team-a/objects/a/b.txt/versions/1",
                      headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["deleted_version"] == 1
    # 删当前版本 v2 -> 409 version_in_use
    r = client.delete("/namespaces/team-a/objects/a/b.txt/versions/2",
                      headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_in_use"
    # 删不存在版本 -> 404 version_not_found
    r = client.delete("/namespaces/team-a/objects/a/b.txt/versions/7",
                      headers=h)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"
    log.info("HTTP 版本删除错误码判定 version_in_use/version_not_found 可区分")


def test_retention_applies_to_deleted_object_history(
        service, make_namespace):
    """已删除对象：保留策略仍裁剪历史，但删除时的最新版本作为留痕保留。"""
    make_namespace(max_history_versions=1)
    for i in range(3):
        _put(service, "gone.bin", f"content-{i}".encode())
    service.delete_object("team-a", "gone.bin")
    # 删除后再写入（重建）触发策略：历史只留最新 1 个
    _put(service, "gone.bin", b"reborn")
    info = service.stat_object("team-a", "gone.bin")
    # 当前 v4 + 最新历史 v3
    assert [v.version for v in info.versions] == [3, 4]
    with pytest.raises(VersionNotFound):
        # v1 已被清理：可区分的 version_not_found
        service.get_object("team-a", "gone.bin", version=1)
    log.info("删除对象保留策略判定 chain=%s",
             [v.version for v in info.versions])
