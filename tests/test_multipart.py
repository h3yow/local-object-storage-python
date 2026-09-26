"""分段上传与断点续传语义测试。

每个用例都打印上传会话 ID、分段判定依据与最终对象版本，便于排查。
"""
from __future__ import annotations

import logging

from app.errors import (
    ObjectChecksumMismatch,
    PartsIncomplete,
    PartChecksumMismatch,
    PartConflict,
    PartOutOfOrder,
    SessionClosed,
    SessionExpired,
    SessionNotFound,
)
from app.models import SessionCreate

from utils import sha256

log = logging.getLogger("object_storage.tests")

PARTS = [b"chunk-001|", b"chunk-002|", b"chunk-003|"]
FULL = b"".join(PARTS)


def _open_session(service, object_name="obj.bin", total_parts=3,
                  final=None, expected_version=None):
    body = SessionCreate(
        object_name=object_name, total_parts=total_parts,
        final_checksum=final or sha256(FULL))
    if expected_version is not None:
        body.expected_version = expected_version
    return service.create_session("team-a", "token-a", body)


def test_full_multipart_flow_and_readback(service, make_namespace, caplog_pretty):
    make_namespace()
    s = _open_session(service)
    log.info("会话已创建 session_id=%s 总分段=%d 对象=%s",
             s.session_id, s.total_parts, s.object_name)
    for i, p in enumerate(PARTS, start=1):
        r = service.upload_part("team-a", s.session_id, i, p, sha256(p))
        assert r["replayed"] is False
        assert r["next_expected"] == i + 1
    res = service.complete_session("team-a", s.session_id)
    log.info("判定依据=%s 产生版本=v%s 结果=%s", res.decision, res.version, res.outcome)
    assert res.version == 1
    assert res.checksum == sha256(FULL)

    vmeta, path, vno = service.get_object("team-a", "obj.bin")
    assert path.read_bytes() == FULL
    assert vno == 1
    # 完成后分段被回收但 session.json 留痕
    assert service.store.load_session("team-a", s.session_id)["status"] == "completed"


def test_interrupted_resume_is_idempotent(service, make_namespace, caplog_pretty):
    """模拟中断：part1 已提交，重放 part1 后继续 part2/part3。"""
    make_namespace()
    s = _open_session(service)
    service.upload_part("team-a", s.session_id, 1, PARTS[0], sha256(PARTS[0]))

    # 客户端重试同一分段（网络重发/断线恢复）
    replay = service.upload_part("team-a", s.session_id, 1, PARTS[0], sha256(PARTS[0]))
    log.info("重复分段判定 session=%s part=1 replayed=%s 依据=checksum一致且为最后序号",
             s.session_id, replay["replayed"])
    assert replay["replayed"] is True

    # 续传剩余分段
    service.upload_part("team-a", s.session_id, 2, PARTS[1], sha256(PARTS[1]))
    service.upload_part("team-a", s.session_id, 3, PARTS[2], sha256(PARTS[2]))
    res = service.complete_session("team-a", s.session_id)
    assert res.checksum == sha256(FULL)
    info = service.stat_object("team-a", "obj.bin")
    log.info("续传完成 对象=%s 当前版本=v%s 历史版本数=%d",
             info.object_name, info.current_version, len(info.versions))
    assert info.current_version == 1


def test_duplicate_part_with_different_checksum_rejected(service, make_namespace):
    make_namespace()
    s = _open_session(service)
    service.upload_part("team-a", s.session_id, 1, PARTS[0], sha256(PARTS[0]))
    # 同序号但不同内容 -> 与幂等重放可区分的冲突错误
    try:
        service.upload_part("team-a", s.session_id, 1, b"different!",
                            sha256(b"different!"))
    except PartConflict as e:
        assert e.error_code == "part_conflict"
        assert e.details["stored"] == sha256(PARTS[0])
        log.info("同序号不同摘要被拒绝 session=%s 依据=%s", s.session_id, e.details)
    else:
        raise AssertionError("expected PartConflict")


def test_part_checksum_mismatch_is_distinguishable(service, make_namespace):
    make_namespace()
    s = _open_session(service)
    try:
        service.upload_part("team-a", s.session_id, 1, PARTS[0], "f" * 64)
    except PartChecksumMismatch as e:
        assert e.error_code == "part_checksum_mismatch"
        assert e.details["actual"] == sha256(PARTS[0])
        log.info("分段摘要错误 part=%s 声明=%s 实际=%s", 1, "f" * 6,
                 e.details["actual"][:6])
    else:
        raise AssertionError("expected PartChecksumMismatch")
    # 失败的分段不被记录：next_expected 仍是 1，且可重试成功
    info = service.get_session("team-a", s.session_id)
    assert info.received_parts == []
    service.upload_part("team-a", s.session_id, 1, PARTS[0], sha256(PARTS[0]))


def test_parts_must_be_strictly_in_order(service, make_namespace):
    make_namespace()
    s = _open_session(service, total_parts=3)
    try:
        service.upload_part("team-a", s.session_id, 2, PARTS[1], sha256(PARTS[1]))
    except PartOutOfOrder as e:
        assert e.error_code == "part_out_of_order"
        assert e.details["expected_next"] == 1
        log.info("非法顺序被拒绝 session=%s 依据=%s", s.session_id, e.details)
    else:
        raise AssertionError("expected PartOutOfOrder")
    # 跳跃提交 part1 后直接 part3 也拒绝
    service.upload_part("team-a", s.session_id, 1, PARTS[0], sha256(PARTS[0]))
    try:
        service.upload_part("team-a", s.session_id, 3, PARTS[2], sha256(PARTS[2]))
    except PartOutOfOrder as e:
        assert e.details["expected_next"] == 2


def test_complete_with_missing_parts_rejected(service, make_namespace):
    make_namespace()
    s = _open_session(service)
    service.upload_part("team-a", s.session_id, 1, PARTS[0], sha256(PARTS[0]))
    try:
        service.complete_session("team-a", s.session_id)
    except PartsIncomplete as e:
        assert e.details["missing"] == [2, 3]
        log.info("未完成拒绝补全 session=%s 缺失=%s", s.session_id, e.details["missing"])
    else:
        raise AssertionError("expected PartsIncomplete")
    # 未完成的上传不得作为对象出现
    assert service.store.get_object_meta("team-a", "obj.bin") is None


def test_final_checksum_mismatch_rejected_and_no_object(service, make_namespace):
    make_namespace()
    s = _open_session(service, final="0" * 64)  # 谎称别的摘要
    for i, p in enumerate(PARTS, start=1):
        service.upload_part("team-a", s.session_id, i, p, sha256(p))
    try:
        service.complete_session("team-a", s.session_id)
    except ObjectChecksumMismatch as e:
        assert e.error_code == "object_checksum_mismatch"
        assert e.details["actual"] == sha256(FULL)
        log.info("对象摘要不符被拒绝 声明=%s 实际=%s", e.details["declared"][:6],
                 e.details["actual"][:6])
    else:
        raise AssertionError("expected ObjectChecksumMismatch")
    # 失败的组装不会留下对象，会话仍 open，允许修正后重试新会话
    assert service.store.get_object_meta("team-a", "obj.bin") is None
    info = service.get_session("team-a", s.session_id)
    assert info.status == "open"


def test_expired_and_closed_sessions_rejected_distinctly(service, make_namespace):
    make_namespace(quota_bytes=0)
    # 用极短 TTL 的服务
    from app.service import ObjectService
    short = ObjectService(service.store.root, session_ttl=-1)
    s = _open_session(short)
    try:
        short.upload_part("team-a", s.session_id, 1, PARTS[0], sha256(PARTS[0]))
    except SessionExpired as e:
        assert e.error_code == "upload_session_expired"
        log.info("会话过期被拒绝 session=%s 依据=expires_at=%s",
                 s.session_id, e.details["expires_at"])
    else:
        raise AssertionError("expected SessionExpired")

    # 已完成会话再提交分段 -> closed，错误可区分
    good = _open_session(service)
    for i, p in enumerate(PARTS, start=1):
        service.upload_part("team-a", good.session_id, i, p, sha256(p))
    service.complete_session("team-a", good.session_id)
    try:
        service.upload_part("team-a", good.session_id, 1, PARTS[0], sha256(PARTS[0]))
    except SessionClosed as e:
        assert e.error_code == "upload_session_closed"
        assert e.details["status"] == "completed"
        log.info("已完成会话拒绝分段 session=%s", good.session_id)
    else:
        raise AssertionError("expected SessionClosed")

    # 未知会话
    import pytest
    with pytest.raises(SessionNotFound):
        service.get_session("team-a", "nope")
