"""命名空间授权、存储配额、删除与回收测试。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import (
    InvalidToken,
    MissingCredentials,
    NamespaceAccessDenied,
    NamespaceNotFound,
    ObjectNotFound,
    QuotaExceeded,
)
from app.models import SessionCreate
from app.service import ObjectService
from utils import sha256

log = logging.getLogger("object_storage.tests")


# ---------------- 授权 ----------------
def test_missing_invalid_and_foreign_token_rejected(client, make_namespace):
    make_namespace(name="ns-a", tokens=["tok-a"])
    make_namespace(name="ns-b", tokens=["tok-b"])

    r = client.get("/namespaces/ns-a/objects")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_credentials"

    r = client.get("/namespaces/ns-a/objects",
                   headers={"X-Namespace-Token": "tok-b"})
    assert r.status_code == 403
    body = r.json()["error"]
    assert body["code"] == "namespace_access_denied"
    log.info("越权访问被拒 ns=ns-a code=%s details=%s", body["code"], body["details"])

    r = client.get("/namespaces/missing/objects",
                   headers={"X-Namespace-Token": "tok-a"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "namespace_not_found"


def test_authorized_token_can_full_cycle(client, make_namespace, uploader):
    result = uploader("a.txt", [b"hello"])
    assert result["complete"].status_code == 201, result["complete"].text
    r = client.get("/namespaces/team-a/objects/a.txt/content",
                   headers={"X-Namespace-Token": "token-a"})
    assert r.status_code == 200 and r.content == b"hello"
    assert r.headers["X-Object-Version"] == "1"


def test_service_level_auth_errors_distinct(service, make_namespace):
    make_namespace()
    with pytest.raises(MissingCredentials):
        service.authorize("team-a", None)
    with pytest.raises(NamespaceAccessDenied):
        service.authorize("team-a", "stranger")
    with pytest.raises(NamespaceNotFound):
        service.authorize("ghost", "x")


# ---------------- 配额 ----------------
def test_quota_blocks_oversized_write_with_partial_exposure(
        service, make_namespace):
    """超配额写入在分段提交时即被拒（在途计费），不产生任何占用与可见对象。"""
    make_namespace(quota_bytes=10)
    body = SessionCreate(object_name="big.bin", total_parts=1,
                         final_checksum=sha256(b"x" * 11))
    s = service.create_session("team-a", "token-a", body)
    with pytest.raises(QuotaExceeded) as ei:
        service.upload_part("team-a", s.session_id, 1, b"x" * 11,
                            sha256(b"x" * 11))
    assert ei.value.error_code == "quota_exceeded"
    assert ei.value.details["projected"] == 11
    assert ei.value.details["inflight"] == 0
    log.info("超配额分段被拒 依据=%s", ei.value.details)
    # 配额未被占用（在途也为 0），且对象不可见
    info = service.get_namespace_info("team-a")
    assert info.used_bytes == 0 and info.inflight_bytes == 0
    assert service.store.get_object_meta("team-a", "big.bin") is None
    # 被拒分段不落盘
    assert not service.store.part_path("team-a", s.session_id, 1).exists()


def test_quota_accounts_for_replaced_version(service, make_namespace):
    """保留 0 个历史版本时：覆盖写稳态只占当前版本大小（旧版本被策略清理），
    但上传期间新旧并存，需要 used+inflight 的瞬时余量。"""
    make_namespace(quota_bytes=16, max_history_versions=0)
    for content in [b"aaaa", b"bbbbbb", b"cccccccccc"]:
        body = SessionCreate(object_name="grow.bin", total_parts=1,
                             final_checksum=sha256(content))
        s = service.create_session("team-a", "token-a", body)
        service.upload_part("team-a", s.session_id, 1, content, sha256(content))
        res = service.complete_session("team-a", s.session_id)
        log.info("配额内覆盖 v%s size=%d used=%s decision=%s", res.version,
                 len(content), service.get_namespace_info("team-a").used_bytes,
                 res.decision)
    # 稳态：只保留当前版本 v3(10B)，v1/v2 已被保留策略清理并释放配额
    ns = service.get_namespace_info("team-a")
    assert ns.used_bytes == 10 and ns.inflight_bytes == 0
    meta = service.stat_object("team-a", "grow.bin")
    assert [v.version for v in meta.versions] == [3]
    # 再增大 1 字节：used(10)+part(11)=21 > 16，分段提交即被拒
    body = SessionCreate(object_name="grow.bin", total_parts=1,
                         final_checksum=sha256(b"d" * 11))
    s = service.create_session("team-a", "token-a", body)
    with pytest.raises(QuotaExceeded):
        service.upload_part("team-a", s.session_id, 1, b"d" * 11,
                            sha256(b"d" * 11))
    # 对象保持上一版本，未出现部分写入
    _, path, v = service.get_object("team-a", "grow.bin")
    assert v == 3 and path.read_bytes() == b"cccccccccc"
    assert service.get_namespace_info("team-a").used_bytes == 10


def test_concurrent_writes_never_breach_quota(service, make_namespace):
    """20 个并发上传各 10 字节，配额 100：在途计费保证恰好 10 个成功，
    其余在分段提交时得到可区分的 quota_exceeded。"""
    make_namespace(quota_bytes=100)
    payloads = [f"c{i:02d}".encode().ljust(10, b"!") for i in range(20)]

    def _upload(item):
        i, payload = item
        name = f"c{i:02d}.bin"
        try:
            body = SessionCreate(object_name=name, total_parts=1,
                                 final_checksum=sha256(payload))
            s = service.create_session("team-a", "token-a", body)
            service.upload_part("team-a", s.session_id, 1, payload,
                                sha256(payload))
            service.complete_session("team-a", s.session_id)
            return ("ok", name)
        except QuotaExceeded as e:
            return ("quota", name, e.error_code)

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(_upload, enumerate(payloads)))
    outcomes = [r for r in results if r[0] == "ok"]
    rejections = [r for r in results if r[0] == "quota"]

    info = service.get_namespace_info("team-a")
    log.info("配额并发判定 成功=%d 拒绝=%d used=%d inflight=%d quota=100 "
             "拒绝原因集合=%s", len(outcomes), len(rejections),
             info.used_bytes, info.inflight_bytes,
             {r[2] for r in rejections})
    assert len(outcomes) == 10
    assert len(rejections) == 10
    assert info.used_bytes == 100 and info.inflight_bytes == 0
    assert all(r[2] == "quota_exceeded" for r in rejections)
    # 被拒对象均不可见
    for _, name, _ in rejections:
        assert service.store.get_object_meta("team-a", name) is None


# ---------------- 删除 / 回收 ----------------
def test_delete_marks_keeps_history_until_versions_pruned(
        service, make_namespace):
    """删除只留标记：全部保留版本继续占配额且内容可读；
    只有清理历史版本才释放配额。"""
    make_namespace(quota_bytes=1000)
    a, b = b"version-one-data", b"v2-data"
    for p in (a, b):
        body = SessionCreate(object_name="d.bin", total_parts=1,
                             final_checksum=sha256(p))
        s = service.create_session("team-a", "token-a", body)
        service.upload_part("team-a", s.session_id, 1, p, sha256(p))
        service.complete_session("team-a", s.session_id)
    # 全部保留版本（v1+v2）都计入配额
    assert service.get_namespace_info("team-a").used_bytes == len(a) + len(b)

    info = service.delete_object("team-a", "d.bin")
    log.info("删除判定=%s", info)
    assert info["history_retained"] == 2
    assert info["bytes_freed"] == 0
    # 删除留痕不释放配额；历史版本内容仍完整可读
    assert service.get_namespace_info("team-a").used_bytes == len(a) + len(b)
    with pytest.raises(ObjectNotFound):
        service.get_object("team-a", "d.bin")
    _, path, v = service.get_object("team-a", "d.bin", version=2)
    assert v == 2 and path.read_bytes() == b
    meta = service.stat_object("team-a", "d.bin")
    assert meta.delete_marker is True and len(meta.versions) == 2

    # 已删除对象没有"生效版本"，两个历史版本都可手动清理，配额同步释放
    r1 = service.delete_version("team-a", "d.bin", 1)
    assert r1["bytes_freed"] == len(a)
    assert service.get_namespace_info("team-a").used_bytes == len(b)
    r2 = service.delete_version("team-a", "d.bin", 2)
    log.info("版本清理判定 r1=%s r2=%s", r1, r2)
    assert service.get_namespace_info("team-a").used_bytes == 0


def test_shared_blob_not_freed_when_referenced(service, make_namespace):
    """内容相同（同 checksum）的两个对象：删一个不能删掉另一个的数据。"""
    make_namespace(quota_bytes=1000)
    payload = b"identical-content"
    for name in ("x.bin", "y.bin"):
        body = SessionCreate(object_name=name, total_parts=1,
                             final_checksum=sha256(payload))
        s = service.create_session("team-a", "token-a", body)
        service.upload_part("team-a", s.session_id, 1, payload, sha256(payload))
        service.complete_session("team-a", s.session_id)
    # 内容寻址：blob 仅一份，配额按逻辑大小累计（2 份引用）
    service.delete_object("team-a", "x.bin")
    _, path, v = service.get_object("team-a", "y.bin")
    assert path.read_bytes() == payload
    log.info("共享 blob 引用计数判定：删除 x.bin 后 y.bin 仍可读 checksum=%s",
             sha256(payload))


def test_purge_expired_sessions_leaves_no_dangling_parts(
        tmp_store, client, make_namespace):
    make_namespace()
    short = ObjectService(tmp_store, session_ttl=-1)
    body = SessionCreate(object_name="z.bin", total_parts=2,
                         final_checksum=sha256(b"ab"))
    s = short.create_session("team-a", "token-a", body)
    short.store.part_path("team-a", s.session_id, 1).parent.mkdir(parents=True,
                                                                  exist_ok=True)
    short.store.part_path("team-a", s.session_id, 1).write_bytes(b"a")
    # TTL 已过：提交被 410 拒绝
    from app.errors import SessionExpired
    with pytest.raises(SessionExpired):
        short.upload_part("team-a", s.session_id, 2, b"b", sha256(b"b"))

    result = short.purge_expired_sessions("team-a")
    assert s.session_id in result["purged"]
    log.info("过期回收判定=%s", result)
    # 分段目录消失，session.json 留痕为 expired，对象数据从未产生
    assert not short.store.part_path("team-a", s.session_id, 1).exists()
    info = short.get_session("team-a", s.session_id)
    assert info.status == "expired"
    assert short.store.get_object_meta("team-a", "z.bin") is None
