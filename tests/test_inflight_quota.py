"""在途字节计入配额测试。

断言：已接收未完成的分段计入命名空间配额；并发上传无法突破上限；
中止/过期/完成时在途占用被正确释放或结转；幂等重放不重复计费；
拒绝原因可区分（reason=inflight_bytes）。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import QuotaExceeded, SessionClosed
from app.models import SessionCreate
from app.service import ObjectService
from utils import sha256

log = logging.getLogger("object_storage.tests")


def _session(service, name: str, data: bytes, *, namespace: str = "team-a",
             total_parts: int = 1):
    body = SessionCreate(object_name=name, total_parts=total_parts,
                         final_checksum=sha256(data))
    return service.create_session(namespace, "token-a", body)


def test_part_upload_charges_inflight_and_complete_settles(
        service, make_namespace):
    """分段提交即在途计费；complete 后在途归零、转为正式占用。"""
    make_namespace(quota_bytes=100)
    p1, p2 = b"a" * 30, b"b" * 20
    s = _session(service, "f.bin", p1 + p2, total_parts=2)
    service.upload_part("team-a", s.session_id, 1, p1, sha256(p1))
    info = service.get_namespace_info("team-a")
    assert info.inflight_bytes == 30 and info.used_bytes == 0
    log.info("在途计费 part1 后 inflight=%d used=%d",
             info.inflight_bytes, info.used_bytes)

    service.upload_part("team-a", s.session_id, 2, p2, sha256(p2))
    assert service.get_namespace_info("team-a").inflight_bytes == 50

    service.complete_session("team-a", s.session_id)
    info = service.get_namespace_info("team-a")
    assert info.inflight_bytes == 0 and info.used_bytes == 50
    log.info("complete 结算 inflight=0 used=%d 判定：在途转正式占用",
             info.used_bytes)


def test_rejected_part_leaves_session_resumable(service, make_namespace):
    """超配额的分段被拒后不占用序号、不计费，同序号可重试。"""
    make_namespace(quota_bytes=10)
    s = _session(service, "r.bin", b"0123456789")
    with pytest.raises(QuotaExceeded) as ei:
        service.upload_part("team-a", s.session_id, 1, b"x" * 11,
                            sha256(b"x" * 11))
    assert ei.value.details["reason"] == "inflight_bytes"
    info = service.get_namespace_info("team-a")
    assert info.inflight_bytes == 0
    # 同序号用合规大小重试成功
    service.upload_part("team-a", s.session_id, 1, b"0123456789",
                        sha256(b"0123456789"))
    r = service.complete_session("team-a", s.session_id)
    assert r.version == 1
    log.info("被拒分段不占序号，重试成功 version=%d", r.version)


def test_idempotent_replay_not_double_charged(service, make_namespace):
    """幂等重放同一分段不重复计入在途。"""
    make_namespace(quota_bytes=100)
    p = b"replay-me"
    s = _session(service, "idem.bin", p)
    service.upload_part("team-a", s.session_id, 1, p, sha256(p))
    out = service.upload_part("team-a", s.session_id, 1, p, sha256(p))
    assert out["replayed"] is True
    assert service.get_namespace_info("team-a").inflight_bytes == len(p)
    log.info("幂等重放 inflight=%d（未重复计费）", len(p))


def test_abort_releases_inflight(service, make_namespace):
    """主动中止：在途占用立即归还。"""
    make_namespace(quota_bytes=100)
    p = b"abort-payload"
    s = _session(service, "a.bin", p)
    service.upload_part("team-a", s.session_id, 1, p, sha256(p))
    assert service.get_namespace_info("team-a").inflight_bytes == len(p)
    service.abort_session("team-a", s.session_id)
    info = service.get_namespace_info("team-a")
    assert info.inflight_bytes == 0 and info.used_bytes == 0
    # 重复中止幂等，不会重复释放成负数
    service.abort_session("team-a", s.session_id)
    assert service.get_namespace_info("team-a").inflight_bytes == 0
    log.info("中止释放在途 inflight=0（幂等）")


def test_expire_and_purge_release_inflight(tmp_store, client, make_namespace):
    """TTL 过期：惰性过期即在途归零；purge 回收分段且不占配额。"""
    make_namespace(quota_bytes=100)
    p = b"expiring"
    normal = ObjectService(tmp_store)
    s2 = _session(normal, "e2.bin", p)
    normal.upload_part("team-a", s2.session_id, 1, p, sha256(p))
    assert normal.get_namespace_info("team-a").inflight_bytes == len(p)
    # 把会话改为已过期
    sess = normal.store.load_session("team-a", s2.session_id)
    sess["expires_at"] = "2000-01-01T00:00:00+00:00"
    normal.store.save_session("team-a", sess)
    # 惰性过期：下一次访问触发，释放在途
    from app.errors import SessionExpired
    with pytest.raises(SessionExpired):
        normal.upload_part("team-a", s2.session_id, 2, b"z", sha256(b"z"))
    assert normal.get_namespace_info("team-a").inflight_bytes == 0
    # purge 幂等：不再重复释放
    out = normal.purge_expired_sessions("team-a")
    assert s2.session_id in out["purged"]
    assert normal.get_namespace_info("team-a").inflight_bytes == 0
    log.info("过期释放在途 purged=%s inflight=0", out["purged"])


def test_failed_complete_keeps_inflight_then_abort_releases(
        service, make_namespace):
    """complete 校验失败（整对象摘要不符）时在途不丢；中止后归还。"""
    make_namespace(quota_bytes=100)
    p = b"checksum-fail"
    body = SessionCreate(object_name="cf.bin", total_parts=1,
                         final_checksum=sha256(b"different"))
    s = service.create_session("team-a", "token-a", body)
    service.upload_part("team-a", s.session_id, 1, p, sha256(p))
    from app.errors import ObjectChecksumMismatch
    with pytest.raises(ObjectChecksumMismatch):
        service.complete_session("team-a", s.session_id)
    # 提交失败：会话仍 open，在途仍计费（数据还在）
    assert service.get_namespace_info("team-a").inflight_bytes == len(p)
    service.abort_session("team-a", s.session_id)
    assert service.get_namespace_info("team-a").inflight_bytes == 0
    log.info("提交失败在途保留，中止后归还 inflight=0")


def test_concurrent_part_uploads_never_breach_quota(service, make_namespace):
    """30 线程并发各传 10 字节、配额 100：恰好 10 个被接收，inflight 恰好 100。"""
    make_namespace(quota_bytes=100)
    sessions = []
    for i in range(30):
        payload = f"p{i:02d}".encode().ljust(10, b"!")
        s = _session(service, f"pc{i:02d}.bin", payload)
        sessions.append((s.session_id, payload))

    accepted, rejected = [], []

    def _push(item):
        sid, payload = item
        try:
            service.upload_part("team-a", sid, 1, payload, sha256(payload))
            return ("ok", sid)
        except QuotaExceeded as e:
            return ("quota", e.details.get("reason"))

    with ThreadPoolExecutor(max_workers=30) as pool:
        for kind, val in pool.map(_push, sessions):
            (accepted if kind == "ok" else rejected).append(val)

    info = service.get_namespace_info("team-a")
    log.info("并发在途判定 接收=%d 拒绝=%d inflight=%d quota=100",
             len(accepted), len(rejected), info.inflight_bytes)
    assert len(accepted) == 10
    assert len(rejected) == 20
    assert all(r == "inflight_bytes" for r in rejected)
    assert info.inflight_bytes == 100 and info.used_bytes == 0

    # 中止一半会话：在途释放一半，剩余会话可继续完成
    for sid in accepted[:5]:
        service.abort_session("team-a", sid)
    assert service.get_namespace_info("team-a").inflight_bytes == 50
    for sid in accepted[5:]:
        service.complete_session("team-a", sid)
    info = service.get_namespace_info("team-a")
    assert info.inflight_bytes == 0 and info.used_bytes == 50
    log.info("中止+完成结算 used=%d inflight=%d", info.used_bytes,
             info.inflight_bytes)


def test_full_quota_still_allows_same_size_overwrite(service, make_namespace):
    """配额占满时，同尺寸覆盖写仍可提交（抵扣本对象当前版本）。"""
    make_namespace(quota_bytes=10)
    p1 = b"0123456789"
    s = _session(service, "ov.bin", p1)
    service.upload_part("team-a", s.session_id, 1, p1, sha256(p1))
    service.complete_session("team-a", s.session_id)
    assert service.get_namespace_info("team-a").used_bytes == 10

    p2 = b"9876543210"  # 同尺寸覆盖：抵扣后 projected=10，允许
    s2 = _session(service, "ov.bin", p2)
    service.upload_part("team-a", s2.session_id, 1, p2, sha256(p2))
    r = service.complete_session("team-a", s2.session_id)
    assert r.version == 2
    info = service.get_namespace_info("team-a")
    assert info.used_bytes == 10 and info.inflight_bytes == 0
    _, path, _ = service.get_object("team-a", "ov.bin")
    assert path.read_bytes() == p2
    log.info("满配额同尺寸覆盖成功 version=%d used=%d", r.version,
             info.used_bytes)


def test_inflight_visible_in_session_and_namespace_info(service, make_namespace,
                                                        client):
    """会话与命名空间视图都暴露在途字节，便于观测。"""
    make_namespace(quota_bytes=100)
    p = b"observe-me"
    s = _session(service, "ob.bin", p)
    service.upload_part("team-a", s.session_id, 1, p, sha256(p))
    sess = service.get_session("team-a", s.session_id)
    assert sess.inflight_bytes == len(p)
    r = client.get("/namespaces/team-a",
                   headers={"X-Namespace-Token": "token-a"})
    assert r.json()["inflight_bytes"] == len(p)
    log.info("在途可观测 session.inflight=%d ns.inflight=%d",
             sess.inflight_bytes, r.json()["inflight_bytes"])
