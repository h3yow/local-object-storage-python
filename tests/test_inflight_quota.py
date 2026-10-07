"""在途字节计入配额测试。

断言：已接收未完成的分段计入 inflight_bytes 并与 used 一起受配额约束；
complete 时在途转为已用；abort/expire/purge 释放占用；提交失败不泄漏；
并发上传无法突破上限；超限原因可区分（quota_exceeded）。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.errors import ObjectChecksumMismatch, QuotaExceeded
from app.models import SessionCreate
from app.store import iso, utcnow
from utils import sha256

log = logging.getLogger("object_storage.tests")


def _session(service, object_name: str, data: bytes, *, total_parts=1,
             final=None):
    body = SessionCreate(object_name=object_name, total_parts=total_parts,
                         final_checksum=final or sha256(data))
    return service.create_session("team-a", "token-a", body)


def _usage(service):
    info = service.get_namespace_info("team-a")
    return info.used_bytes, info.inflight_bytes


def test_inflight_counted_then_converted_on_complete(
        service, make_namespace, caplog_pretty):
    """分段到达即占配额；complete 后在途转为已用，总量不变。"""
    make_namespace(quota_bytes=100)
    p1, p2 = b"a" * 30, b"b" * 30
    s = _session(service, "f.bin", p1 + p2, total_parts=2)

    service.upload_part("team-a", s.session_id, 1, p1, sha256(p1))
    assert _usage(service) == (0, 30)
    service.upload_part("team-a", s.session_id, 2, p2, sha256(p2))
    used, inflight = _usage(service)
    log.info("分段到齐 sid=%s used=%d inflight=%d", s.session_id, used,
             inflight)
    assert (used, inflight) == (0, 60)
    # 会话视图同样可见已接收字节
    assert service.get_session("team-a", s.session_id).received_bytes == 60

    res = service.complete_session("team-a", s.session_id)
    used, inflight = _usage(service)
    log.info("complete 后 version=v%d used=%d inflight=%d decision=%s",
             res.version, used, inflight, res.decision)
    assert (used, inflight) == (60, 0)


def test_inflight_blocks_second_session_until_abort(
        service, make_namespace, caplog_pretty):
    """A 的在途 40B + B 的 20B > 配额 50：B 被拒；A 中止后 B 成功。"""
    make_namespace(quota_bytes=50)
    sa = _session(service, "a.bin", b"a" * 40)
    service.upload_part("team-a", sa.session_id, 1, b"a" * 40,
                        sha256(b"a" * 40))
    assert _usage(service) == (0, 40)

    sb = _session(service, "b.bin", b"b" * 20)
    with pytest.raises(QuotaExceeded) as ei:
        service.upload_part("team-a", sb.session_id, 1, b"b" * 20,
                            sha256(b"b" * 20))
    assert ei.value.error_code == "quota_exceeded"
    assert ei.value.details["inflight"] == 40
    assert ei.value.details["projected"] == 60
    log.info("在途挤占判定 依据=%s", ei.value.details)

    # A 主动中止：在途占用立即归还，B 重试成功
    service.abort_session("team-a", sa.session_id)
    assert _usage(service) == (0, 0)
    service.upload_part("team-a", sb.session_id, 1, b"b" * 20,
                        sha256(b"b" * 20))
    service.complete_session("team-a", sb.session_id)
    assert _usage(service) == (20, 0)


def _force_expire(service, session_id: str) -> None:
    """把会话的 expires_at 改到过去（模拟 TTL 到期）。"""
    s = service.store.load_session("team-a", session_id)
    s["expires_at"] = iso(utcnow() - timedelta(seconds=1))
    service.store.save_session("team-a", s)


def test_inflight_released_on_lazy_expire_and_purge(
        service, make_namespace, caplog_pretty):
    """过期会话的在途占用：惰性过期与定期回收都必须归还。"""
    make_namespace(quota_bytes=100)
    # 路径 1：访问时惰性过期
    s1 = _session(service, "e1.bin", b"x" * 25)
    service.upload_part("team-a", s1.session_id, 1, b"x" * 25,
                        sha256(b"x" * 25))
    assert _usage(service) == (0, 25)
    _force_expire(service, s1.session_id)
    info = service.get_session("team-a", s1.session_id)
    assert info.status == "expired"
    assert _usage(service) == (0, 0)
    log.info("惰性过期释放 sid=%s inflight=0", s1.session_id)

    # 路径 2：maintenance 回收
    s2 = _session(service, "e2.bin", b"y" * 25)
    service.upload_part("team-a", s2.session_id, 1, b"y" * 25,
                        sha256(b"y" * 25))
    assert _usage(service) == (0, 25)
    _force_expire(service, s2.session_id)
    result = service.purge_expired_sessions("team-a")
    assert s2.session_id in result["purged"]
    assert _usage(service) == (0, 0)
    log.info("回收释放判定=%s", result)


def test_failed_complete_keeps_inflight_until_abort(
        service, make_namespace, caplog_pretty):
    """提交失败（摘要不符）：分段仍在会话中，在途占用不泄漏也不提前释放；
    中止后才归还。"""
    make_namespace(quota_bytes=100)
    data = b"z" * 30
    s = _session(service, "bad.bin", data, final="0" * 64)  # 谎称摘要
    service.upload_part("team-a", s.session_id, 1, data, sha256(data))
    assert _usage(service) == (0, 30)
    with pytest.raises(ObjectChecksumMismatch):
        service.complete_session("team-a", s.session_id)
    # 会话仍 open，分段未回收，在途仍计入（不能无声消失）
    assert service.get_session("team-a", s.session_id).status == "open"
    assert _usage(service) == (0, 30)
    service.abort_session("team-a", s.session_id)
    used, inflight = _usage(service)
    log.info("提交失败后中止 sid=%s used=%d inflight=%d", s.session_id, used,
             inflight)
    assert (used, inflight) == (0, 0)


def test_multipart_sessions_fill_quota_exactly(service, make_namespace,
                                               caplog_pretty):
    """10 个多分段会话各 10B，配额 100：全部成功且 used 恰好 100，
    第 11 个在分段提交时被拒。"""
    make_namespace(quota_bytes=100)
    for i in range(10):
        p1, p2 = b"m" * 5, b"n" * 5
        s = _session(service, f"m{i}.bin", p1 + p2, total_parts=2)
        service.upload_part("team-a", s.session_id, 1, p1, sha256(p1))
        service.upload_part("team-a", s.session_id, 2, p2, sha256(p2))
        service.complete_session("team-a", s.session_id)
    assert _usage(service) == (100, 0)

    s = _session(service, "overflow.bin", b"o")
    with pytest.raises(QuotaExceeded) as ei:
        service.upload_part("team-a", s.session_id, 1, b"o", sha256(b"o"))
    assert ei.value.details["used"] == 100
    log.info("边界判定 used=100 quota=100 第 11 个会话被拒 依据=%s",
             ei.value.details)


def test_concurrent_inflight_never_breaches_quota(
        service, make_namespace, caplog_pretty):
    """并发边界：12 个会话并发传 20B（2×10B），配额 100。

    不变式：任一时刻 used+inflight<=100；成功完成数 <= 5（5*20=100）；
    每个成功者恰好贡献 20B；被拒者拿到可区分的 quota_exceeded；
    中止残留在途会话后 inflight 归零、无泄漏。
    """
    make_namespace(quota_bytes=100)
    p1, p2 = b"p" * 10, b"q" * 10

    def _upload(i):
        name = f"r{i:02d}.bin"
        s = _session(service, name, p1 + p2, total_parts=2)
        try:
            service.upload_part("team-a", s.session_id, 1, p1, sha256(p1))
            service.upload_part("team-a", s.session_id, 2, p2, sha256(p2))
            service.complete_session("team-a", s.session_id)
            return ("ok", name, s.session_id)
        except QuotaExceeded:
            return ("quota", name, s.session_id)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(_upload, range(12)))
    ok = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "quota"]
    used, inflight = _usage(service)
    log.info("并发在途边界判定 成功=%d 拒绝=%d used=%d inflight=%d",
             len(ok), len(rejected), used, inflight)
    # 核心不变式：从未突破配额；每个完成者恰好 20B
    assert len(ok) <= 5
    assert used == 20 * len(ok)
    assert used + inflight <= 100
    assert len(ok) + len(rejected) == 12

    # 残留在途会话中止后：占用全部归还，不越积越多
    for kind, _, sid in results:
        if kind == "quota":
            service.abort_session("team-a", sid)
    used, inflight = _usage(service)
    log.info("清理后 used=%d inflight=%d", used, inflight)
    assert inflight == 0
    assert used == 20 * len(ok)
