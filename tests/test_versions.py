"""对象版本与并发覆盖语义测试。

断言：同名覆盖产生可回溯版本；并发提交有确定胜负；旧版本不会无解释消失；
乐观并发失败返回可区分的 version_conflict。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.errors import VersionConflict
from app.models import SessionCreate
from utils import sha256

log = logging.getLogger("object_storage.tests")


def _start(service, object_name: str, data: bytes, *, total_parts: int = 1,
           expected_version=None):
    body = SessionCreate(
        object_name=object_name, total_parts=total_parts,
        final_checksum=sha256(data),
        metadata={"payload": data.decode(errors="replace")})
    if expected_version is not None:
        body.expected_version = expected_version
    s = service.create_session("team-a", "token-a", body)
    return s


def _push_part(service, sid: str, data: bytes, seq: int = 1) -> None:
    service.upload_part("team-a", sid, seq, data, sha256(data))


def test_overwrite_creates_traceable_versions(service, make_namespace):
    make_namespace()
    v1 = b"first-version-content"
    v2 = b"second-version-content-longer"
    v3 = b"v3"

    s1 = _start(service, "doc.txt", v1)
    _push_part(service, s1.session_id, v1)
    r1 = service.complete_session("team-a", s1.session_id)
    assert r1.outcome == "created" and r1.version == 1

    s2 = _start(service, "doc.txt", v2)
    _push_part(service, s2.session_id, v2)
    r2 = service.complete_session("team-a", s2.session_id)
    log.info("覆盖判定=%s 新版本=v%s 被替换版本=v%s",
             r2.decision, r2.version, r2.replaced_version)
    assert r2.outcome == "replaced"
    assert r2.version == 2 and r2.replaced_version == 1

    s3 = _start(service, "doc.txt", v3)
    _push_part(service, s3.session_id, v3)
    r3 = service.complete_session("team-a", s3.session_id)
    assert r3.version == 3

    info = service.stat_object("team-a", "doc.txt")
    assert [v.version for v in info.versions] == [1, 2, 3]
    # 每个历史版本都可按版本读取且内容正确
    for vno, expect in [(1, v1), (2, v2), (3, v3)]:
        meta, path, n = service.get_object("team-a", "doc.txt", version=vno)
        assert path.read_bytes() == expect and n == vno
    # 默认读取最新
    _, path, cur = service.get_object("team-a", "doc.txt")
    assert path.read_bytes() == v3 and cur == 3
    log.info("版本链可回溯 object=doc.txt versions=%s current=v%s",
             [v.version for v in info.versions], info.current_version)


def test_concurrent_overwrite_has_deterministic_winner(service, make_namespace):
    """10 个会话并发完成同一对象：恰好产生 1..10 连续版本，无一丢失。"""
    make_namespace()
    object_name = "race.bin"

    # 初始版本
    base = b"v0-base"
    s0 = _start(service, object_name, base)
    _push_part(service, s0.session_id, base)
    service.complete_session("team-a", s0.session_id)

    payloads = [f"concurrent-content-{i:02d}".encode() for i in range(10)]
    sessions = []
    for p in payloads:
        s = _start(service, object_name, p)
        _push_part(service, s.session_id, p)
        sessions.append((s.session_id, p))

    results, errors = [], []

    def _commit(item):
        sid, p = item
        try:
            return service.complete_session("team-a", sid)
        except Exception as e:  # noqa: BLE001 - 测试需要观察所有结果
            return e

    with ThreadPoolExecutor(max_workers=10) as pool:
        outs = list(pool.map(_commit, sessions))
    for o in outs:
        if isinstance(o, Exception):
            errors.append(o)
        else:
            results.append(o)

    log.info("并发覆盖完成 成功=%d 失败=%d", len(results), len(errors))
    assert errors == []  # 无条件覆盖：锁串行化后都应成功
    versions = sorted(r.version for r in results)
    assert versions == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11], versions

    # 每个成功提交都必须在版本链中可回溯，旧版本未丢失
    info = service.stat_object("team-a", object_name)
    chain = [v.version for v in info.versions]
    assert chain == list(range(1, 12)), chain
    checksums = {r.checksum for r in results}
    stored = {v.checksum for v in info.versions[1:]}
    assert checksums <= stored
    # 最新版本恰好是某一个并发写入，且默认读取到它
    _, path, cur = service.get_object("team-a", object_name)
    assert cur == 11 and path.read_bytes() in payloads
    log.info("并发判定依据：store RLock 串行化提交, version=max+1, 最终胜出=v%s(%s)",
             cur, path.read_bytes())


def test_optimistic_concurrency_loser_gets_distinguishable_error(
        service, make_namespace):
    """expected_version 过期的请求必须被拒绝，错误原因=version_conflict。"""
    make_namespace()
    object_name = "cc.txt"
    v1, v2, v3 = b"one", b"two", b"three"

    s1 = _start(service, object_name, v1, expected_version=0)
    _push_part(service, s1.session_id, v1)
    r1 = service.complete_session("team-a", s1.session_id)
    assert r1.version == 1

    # 两个客户端都基于 v1 发起条件覆盖
    sa = _start(service, object_name, v2, expected_version=1)
    sb = _start(service, object_name, v3, expected_version=1)
    _push_part(service, sa.session_id, v2)
    _push_part(service, sb.session_id, v3)

    ra = service.complete_session("team-a", sa.session_id)
    assert ra.version == 2
    try:
        service.complete_session("team-a", sb.session_id)
    except VersionConflict as e:
        assert e.error_code == "version_conflict"
        assert e.details == {"expected_version": 1, "actual_version": 2}
        log.info("乐观并发失败者被拒 依据=%s", e.details)
    else:
        raise AssertionError("expected VersionConflict")

    # 失败方没有暴露部分写入：对象当前仍是 v2，版本链 1,2
    info = service.stat_object("team-a", object_name)
    assert info.current_version == 2
    assert [v.version for v in info.versions] == [1, 2]
    _, path, _ = service.get_object("team-a", object_name)
    assert path.read_bytes() == v2


def test_expected_version_zero_on_existing_object_rejected(service, make_namespace):
    make_namespace()
    p = b"data"
    s = _start(service, "new.obj", p, expected_version=0)
    _push_part(service, s.session_id, p)
    service.complete_session("team-a", s.session_id)

    s2 = _start(service, "new.obj", b"other", expected_version=0)
    _push_part(service, s2.session_id, b"other")
    try:
        service.complete_session("team-a", s2.session_id)
    except VersionConflict as e:
        assert e.details["actual_version"] == 1
        log.info("对象已存在时 expected_version=0 被拒: %s", e.details)
    else:
        raise AssertionError("expected VersionConflict")


def test_recreate_after_delete_keeps_history(service, make_namespace):
    """删除后重建：历史版本保留，新版本号在历史之上递增。"""
    make_namespace()
    name = "lifecycle.txt"
    s1 = _start(service, name, b"a")
    _push_part(service, s1.session_id, b"a")
    service.complete_session("team-a", s1.session_id)
    service.delete_object("team-a", name)

    s2 = _start(service, name, b"b")  # 不带 expected_version，允许删除后重建
    _push_part(service, s2.session_id, b"b")
    r = service.complete_session("team-a", s2.session_id)
    assert r.outcome == "created" and r.version == 2
    info = service.stat_object("team-a", name)
    assert [v.version for v in info.versions] == [1, 2]
    assert info.delete_marker is False
    log.info("删除后重建 object=%s versions=%s current=v%s",
             name, [1, 2], info.current_version)
