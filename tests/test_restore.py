"""按版本恢复测试。

断言：任意历史版本（含已删除对象的）可恢复为新当前版本；内容/类型/元信息
与源版本一致；版本号继续递增、历史不丢；默认下载即恢复后内容；
对象不存在 / 版本不存在 / 对象已删除三种情况可区分；
并发恢复/删除/上传结果确定且失败方原因明确。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import ObjectNotFound, VersionNotFound
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


def test_restore_old_version_becomes_new_current(service, make_namespace):
    """恢复 v1：生成 v4，内容/类型/元信息与 v1 一致，历史一条不丢。"""
    make_namespace()
    _put(service, "r.txt", b"alpha", content_type="text/plain",
         metadata={"author": "alice"})
    _put(service, "r.txt", b"beta")
    _put(service, "r.txt", b"gamma")

    res = service.restore_object("team-a", "r.txt", 1)
    log.info("恢复判定=%s", res.decision)
    assert res.version == 4 and res.restored_from == 1
    assert res.was_delete_marker is False
    assert res.checksum == sha256(b"alpha")

    info = service.stat_object("team-a", "r.txt")
    assert [v.version for v in info.versions] == [1, 2, 3, 4]
    assert info.current_version == 4 and info.delete_marker is False
    v4 = next(v for v in info.versions if v.version == 4)
    assert v4.restored_from == 1
    assert v4.content_type == "text/plain"
    assert v4.metadata == {"author": "alice"}
    # 默认下载即恢复后的内容
    meta, path, cur = service.get_object("team-a", "r.txt")
    assert cur == 4 and path.read_bytes() == b"alpha"
    assert meta["content_type"] == "text/plain"
    # 历史版本仍可读
    _, p2, _ = service.get_object("team-a", "r.txt", version=2)
    assert p2.read_bytes() == b"beta"


def test_restore_deleted_object_distinguishable(service, make_namespace):
    """已删除对象可恢复：was_delete_marker=True，删除标记被清除。"""
    make_namespace()
    _put(service, "d.txt", b"first")
    _put(service, "d.txt", b"second")
    service.delete_object("team-a", "d.txt")
    # 删除后默认读 404
    with pytest.raises(ObjectNotFound):
        service.get_object("team-a", "d.txt")

    res = service.restore_object("team-a", "d.txt", 1)
    log.info("删除后恢复判定=%s", res.decision)
    assert res.was_delete_marker is True   # 与"对象本就存活"可区分
    assert res.version == 3 and res.restored_from == 1

    info = service.stat_object("team-a", "d.txt")
    assert info.delete_marker is False
    assert [v.version for v in info.versions] == [1, 2, 3]
    _, path, cur = service.get_object("team-a", "d.txt")
    assert cur == 3 and path.read_bytes() == b"first"


def test_restore_error_cases_are_distinguishable(service, make_namespace):
    """对象不存在 / 版本不存在 / 对象已删除三种情况互相可区分。"""
    make_namespace()
    _put(service, "e.txt", b"only")

    with pytest.raises(ObjectNotFound) as eo:
        service.restore_object("team-a", "ghost.txt", 1)
    assert eo.value.error_code == "object_not_found"

    with pytest.raises(VersionNotFound) as ev:
        service.restore_object("team-a", "e.txt", 99)
    assert ev.value.error_code == "version_not_found"
    assert ev.value.details["available"] == [1]

    service.delete_object("team-a", "e.txt")
    res = service.restore_object("team-a", "e.txt", 1)
    assert res.was_delete_marker is True  # 已删除 -> 成功且响应可区分
    log.info("三类情况区分：object_not_found / version_not_found / "
             "was_delete_marker=True")


def test_restore_then_overwrite_version_continues(service, make_namespace):
    """恢复后继续上传：版本号在恢复结果之上递增，不重排不重用。"""
    make_namespace()
    _put(service, "c.bin", b"c1")
    _put(service, "c.bin", b"c2")
    res = service.restore_object("team-a", "c.bin", 1)
    assert res.version == 3
    r = _put(service, "c.bin", b"c3")
    assert r.version == 4
    info = service.stat_object("team-a", "c.bin")
    assert [v.version for v in info.versions] == [1, 2, 3, 4]
    _, path, _ = service.get_object("team-a", "c.bin")
    assert path.read_bytes() == b"c3"


def test_restore_counts_toward_quota(service, make_namespace):
    """恢复产生的新当前版本计入配额：恢复大版本可能超配额被拒。"""
    make_namespace(quota_bytes=10)
    _put(service, "q.bin", b"1234567890")   # v1: 10 字节, used=10
    _put(service, "q.bin", b"xy")           # v2: 2 字节, used=2
    # 恢复 v1（10 字节）：projected = 2 - 2 + 10 = 10，恰好允许
    res = service.restore_object("team-a", "q.bin", 1)
    assert res.version == 3
    assert service.get_namespace_info("team-a").used_bytes == 10
    log.info("恢复计入配额 used=%d quota=10 判定=%s",
             service.get_namespace_info("team-a").used_bytes, res.decision)


def test_concurrent_restore_delete_upload_deterministic(service, make_namespace):
    """并发 恢复v1 / 删除 / 上传新版本：锁串行化后结果确定、可解释。

    无论时序如何，最终对象要么存活（恢复或上传最后执行）、要么带删除
    标记（删除最后执行）；版本链单调、无丢失、无半写内容；每个版本
    都可按版本号读到完整内容。
    """
    make_namespace()
    _put(service, "race.bin", b"base-v1")
    _put(service, "race.bin", b"base-v2")

    outcomes = {}

    def do_restore():
        try:
            r = service.restore_object("team-a", "race.bin", 1)
            return ("restore_ok", r.version)
        except Exception as e:  # noqa: BLE001
            return ("restore_err", getattr(e, "error_code", str(e)))

    def do_delete():
        try:
            service.delete_object("team-a", "race.bin")
            return ("delete_ok", None)
        except Exception as e:  # noqa: BLE001
            return ("delete_err", getattr(e, "error_code", str(e)))

    def do_upload():
        try:
            r = _put(service, "race.bin", b"concurrent-upload")
            return ("upload_ok", r.version)
        except Exception as e:  # noqa: BLE001
            return ("upload_err", getattr(e, "error_code", str(e)))

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(do_restore), pool.submit(do_delete),
                pool.submit(do_upload)]
        for f in futs:
            kind, val = f.result()
            outcomes[kind] = val
    log.info("并发恢复/删除/上传结果=%s", outcomes)

    # 三类操作各自成功（删除/恢复/上传在锁内串行，无版本冲突类失败）
    assert "restore_ok" in outcomes and "delete_ok" in outcomes
    assert "upload_ok" in outcomes

    info = service.stat_object("team-a", "race.bin")
    chain = [v.version for v in info.versions]
    # 版本链：v1,v2 + 恢复产生的新版本 + 上传产生的新版本，连续单调
    assert chain == sorted(chain) and len(chain) == 4
    assert chain[0] == 1 and chain[-1] == info.current_version
    # 凡在链上的版本都读得到完整内容
    for v in info.versions:
        _, path, n = service.get_object("team-a", "race.bin", version=v.version)
        assert n == v.version
        assert path.read_bytes() in (b"base-v1", b"base-v2",
                                     b"concurrent-upload")
    # 最终状态确定：删除标记只取决于锁序中 delete 是否最后生效；
    # 若 delete 之后还有 restore/upload，对象必然存活
    log.info("最终版本链=%s current=v%s delete_marker=%s",
             chain, info.current_version, info.delete_marker)


def test_restore_loser_when_version_pruned_concurrently(
        service, make_namespace):
    """恢复与策略收紧并发：若目标版本先被清理，恢复方得到 version_not_found。"""
    make_namespace()  # 不限保留
    for i in range(5):
        _put(service, "p.bin", f"p-{i}".encode())

    results = []

    def do_restore():
        try:
            r = service.restore_object("team-a", "p.bin", 1)
            results.append(("restore_ok", r.version))
        except VersionNotFound as e:
            results.append(("restore_version_not_found",
                            e.details["requested_version"]))

    def do_tighten():
        service.update_policy("team-a", 1)  # 只留当前版本，v1..v4 被清
        results.append(("policy_ok", None))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [do_restore, do_tighten]))

    log.info("恢复vs清理并发结果=%s", results)
    kinds = {k for k, _ in results}
    assert "policy_ok" in kinds
    # 两种确定结局：恢复先到 -> 产生新版本；清理先到 -> version_not_found
    assert kinds & {"restore_ok", "restore_version_not_found"}
    info = service.stat_object("team-a", "p.bin")
    if "restore_ok" in kinds:
        # 恢复成功后策略仍生效：链上版本数 <= 1（当前），且恢复版即当前
        assert info.current_version >= 6
    else:
        assert [v.version for v in info.versions] == [5]
    # 链上版本全部可读
    for v in info.versions:
        _, path, _ = service.get_object("team-a", "p.bin", version=v.version)
        assert path.read_bytes()
