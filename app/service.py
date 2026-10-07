"""业务服务层：会话编排、幂等分段、版本判定、配额与审计。

并发模型：部署为单进程，FastAPI 同步路由 + 线程池执行；store 的 RLock 保证
所有"读-判定-写"序列串行，因此覆盖胜负确定、配额不会超限。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from . import errors
from .models import (
    CompleteResult,
    NamespaceCreate,
    NamespaceInfo,
    ObjectInfo,
    RestoreRequest,
    RestoreResult,
    SessionCreate,
    SessionInfo,
    VersionInfo,
)
from .store import LocalStore, iso, parse_iso, utcnow

log = logging.getLogger("object_storage")

SHA256_LEN = 64
SESSION_TTL_SECONDS = 24 * 3600


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _bytes_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ObjectService:
    def __init__(self, root: str | Path, *, session_ttl: int = SESSION_TTL_SECONDS) -> None:
        self.store = LocalStore(root)
        self.session_ttl = session_ttl
        # 启动对账：以磁盘上的版本链/会话为真相重算 used/inflight，
        # 兼容旧版本落盘的数据（used 口径已从"仅当前版本"变为"全部保留版本"）。
        with self.store.lock():
            for ns in self.store.list_namespaces():
                rec = self.store.reconcile_namespace_usage(ns)
                log.info("ns_reconcile ns=%s used=%d inflight=%d",
                         ns, rec["used_bytes"], rec["inflight_bytes"])

    # ================= 配额占用（在途字节） =================
    def _charge_inflight(self, namespace: str, session: dict[str, Any],
                         size: int) -> None:
        """把新接收的分段字节计入命名空间在途占用；超配额则拒绝。

        判定不变式：used_bytes + inflight_bytes + 本次分段 <= quota。
        所有已接收分段都已计费，因此 complete 时无需再次检查配额。
        """
        nsrec = self.store.get_namespace(namespace)
        assert nsrec is not None
        quota = nsrec.get("quota_bytes", 0) or 0
        used = nsrec.get("used_bytes", 0)
        inflight = nsrec.get("inflight_bytes", 0)
        projected = used + inflight + size
        if quota and projected > quota:
            decision = (f"quota:used={used},inflight={inflight},"
                        f"part={size},projected={projected},quota={quota}")
            self.store.append_audit(
                {"action": "part_reject", "namespace": namespace,
                 "session_id": session["session_id"],
                 "reason": "quota_exceeded", "decision": decision})
            log.warning("part_reject sid=%s %s", session["session_id"], decision)
            raise errors.QuotaExceeded(
                "accepting this part would exceed namespace quota",
                details={"used": used, "inflight": inflight, "quota": quota,
                         "projected": projected, "incoming_size": size},
            )
        session["inflight_bytes"] = session.get("inflight_bytes", 0) + size
        self.store.update_namespace(namespace, inflight_bytes=inflight + size)

    def _release_session_inflight(self, namespace: str,
                                  session: dict[str, Any]) -> int:
        """释放会话的在途占用（complete/abort/expire/purge）。幂等。"""
        amount = int(session.get("inflight_bytes", 0) or 0)
        if amount:
            session["inflight_bytes"] = 0
            nsrec = self.store.get_namespace(namespace)
            assert nsrec is not None
            self.store.update_namespace(
                namespace,
                inflight_bytes=max(0, nsrec.get("inflight_bytes", 0) - amount))
        return amount

    # ================= 命名空间 / 授权 =================
    def create_namespace(self, body: NamespaceCreate) -> NamespaceInfo:
        with self.store.lock():
            if self.store.namespace_exists(body.name):
                raise errors.ServiceError(
                    f"namespace {body.name!r} already exists",
                )
            rec = self.store.create_namespace(
                body.name, body.quota_bytes, body.tokens,
                body.max_history_versions)
            self.store.append_audit(
                {"action": "namespace_create", "namespace": body.name,
                 "quota_bytes": body.quota_bytes, "tokens": body.tokens,
                 "max_history_versions": body.max_history_versions}
            )
            log.info("namespace_create name=%s quota=%s tokens=%d "
                     "max_history_versions=%s",
                     body.name, body.quota_bytes, len(body.tokens),
                     body.max_history_versions)
            return self._ns_info(rec)

    def get_namespace_info(self, namespace: str) -> NamespaceInfo:
        rec = self.store.get_namespace(namespace)
        if rec is None:
            raise errors.NamespaceNotFound(f"namespace {namespace!r} not found")
        return self._ns_info(rec)

    @staticmethod
    def _ns_info(rec: dict[str, Any]) -> NamespaceInfo:
        return NamespaceInfo(
            name=rec["name"], quota_bytes=rec["quota_bytes"],
            used_bytes=rec.get("used_bytes", 0),
            inflight_bytes=rec.get("inflight_bytes", 0),
            max_history_versions=rec.get("max_history_versions"),
            tokens=list(rec.get("tokens", [])),
        )

    def authorize(self, namespace: str, token: str | None) -> dict[str, Any]:
        """解析命名空间并校验令牌，返回命名空间记录。"""
        if token is None or token == "":
            raise errors.MissingCredentials("namespace token is required")
        rec = self.store.get_namespace(namespace)
        if rec is None:
            raise errors.NamespaceNotFound(f"namespace {namespace!r} not found")
        if token not in rec.get("tokens", []):
            raise errors.NamespaceAccessDenied(
                "token not authorized for namespace",
                details={"namespace": namespace},
            )
        return rec

    # ================= 会话生命周期 =================
    def create_session(self, namespace: str, token: str,
                       body: SessionCreate) -> SessionInfo:
        with self.store.lock():
            self.authorize(namespace, token)
            if len(body.final_checksum) != SHA256_LEN:
                raise errors.ServiceError("final_checksum must be 64-char sha256 hex")
            sid = uuid.uuid4().hex
            created, expires = self.store.ttl_expiry(self.session_ttl)
            session = {
                "session_id": sid,
                "namespace": namespace,
                "object_name": body.object_name,
                "total_parts": body.total_parts,
                "final_checksum": body.final_checksum.lower(),
                "content_type": body.content_type,
                "metadata": body.metadata,
                "expected_version": body.expected_version,
                "parts": {},  # seq -> {"checksum": ..., "size": ...}
                "inflight_bytes": 0,  # 已计入命名空间配额的已接收分段字节
                "status": "open",
                "created_at": created,
                "expires_at": expires,
            }
            self.store.save_session(namespace, session)
            self.store.append_audit(
                {"action": "session_create", "namespace": namespace,
                 "session_id": sid, "object": body.object_name,
                 "total_parts": body.total_parts,
                 "final_checksum": body.final_checksum,
                 "expected_version": body.expected_version}
            )
            log.info("session_create sid=%s ns=%s object=%s total_parts=%d "
                     "expected_version=%s", sid, namespace, body.object_name,
                     body.total_parts, body.expected_version)
            return self._session_info(session)

    def _get_open_session(self, namespace: str, session_id: str) -> dict[str, Any]:
        session = self.store.load_session(namespace, session_id)
        if session is None:
            raise errors.SessionNotFound(f"upload session {session_id!r} not found")
        if session["status"] == "completed":
            raise errors.SessionClosed("upload session already completed",
                                       details={"session_id": session_id,
                                                "status": "completed"})
        if session["status"] == "aborted":
            raise errors.SessionClosed("upload session was aborted",
                                       details={"session_id": session_id,
                                                "status": "aborted"})
        if parse_iso(session["expires_at"]) <= utcnow():
            # 惰性过期：改状态、回收分段并归还在途配额（调用方须持锁）。
            session["status"] = "expired"
            released = self._release_session_inflight(namespace, session)
            self.store.save_session(namespace, session)
            self.store.remove_session_parts(namespace, session_id)
            self.store.append_audit(
                {"action": "session_expire", "namespace": namespace,
                 "session_id": session_id,
                 "decision": f"ttl_expired,inflight_released={released}"})
            raise errors.SessionExpired(
                "upload session expired",
                details={"session_id": session_id,
                         "expires_at": session["expires_at"]},
            )
        return session

    def get_session(self, namespace: str, session_id: str) -> SessionInfo:
        session = self.store.load_session(namespace, session_id)
        if session is None:
            raise errors.SessionNotFound(f"upload session {session_id!r} not found")
        # 读取时顺带做惰性过期判定，保证状态可解释
        if session["status"] == "open" and parse_iso(session["expires_at"]) <= utcnow():
            with self.store.lock():
                session["status"] = "expired"
                self._release_session_inflight(namespace, session)
                self.store.save_session(namespace, session)
                self.store.remove_session_parts(namespace, session_id)
        return self._session_info(session)

    def abort_session(self, namespace: str, session_id: str) -> SessionInfo:
        with self.store.lock():
            session = self.store.load_session(namespace, session_id)
            if session is None:
                raise errors.SessionNotFound(f"upload session {session_id!r} not found")
            if session["status"] in ("open", "expired"):
                session["status"] = "aborted"
                released = self._release_session_inflight(namespace, session)
                self.store.save_session(namespace, session)
                self.store.append_audit(
                    {"action": "session_abort", "namespace": namespace,
                     "session_id": session_id,
                     "decision": f"aborted,parts_reclaimed,"
                                 f"inflight_released={released}"})
            self.store.remove_session_parts(namespace, session_id)
            log.info("session_abort sid=%s ns=%s status=%s", session_id,
                     namespace, session["status"])
            return self._session_info(session)

    # ================= 分段提交（幂等） =================
    def upload_part(self, namespace: str, session_id: str, seq: int,
                    data: bytes, declared_checksum: str) -> dict[str, Any]:
        if seq < 1:
            raise errors.ServiceError("part sequence must be >= 1")
        if len(declared_checksum) != SHA256_LEN:
            raise errors.ServiceError("checksum must be 64-char sha256 hex")
        declared_checksum = declared_checksum.lower()
        actual = _bytes_hex(data)
        if actual != declared_checksum:
            raise errors.PartChecksumMismatch(
                "part payload does not match declared sha256",
                details={"part": seq, "declared": declared_checksum, "actual": actual},
            )
        with self.store.lock():
            session = self._get_open_session(namespace, session_id)
            total = session["total_parts"]
            parts: dict[str, Any] = session["parts"]
            received = sorted(int(k) for k in parts)

            # 幂等重放：该序号恰好是已提交的最后一个分段，摘要一致 -> 直接返回
            existing = parts.get(str(seq))
            if existing is not None:
                if existing["checksum"] == declared_checksum and existing["size"] == len(data):
                    log.info("part_idempotent sid=%s part=%d checksum=%s",
                             session_id, seq, declared_checksum)
                    self.store.append_audit(
                        {"action": "part_replay", "namespace": namespace,
                         "session_id": session_id, "part": seq,
                         "decision": "idempotent_replay",
                         "checksum": declared_checksum})
                    return {"part": seq, "checksum": declared_checksum,
                            "replayed": True,
                            "next_expected": (max(received) + 1) if received else 1}
                raise errors.PartConflict(
                    "same part number submitted with different checksum",
                    details={"part": seq, "stored": existing["checksum"],
                             "declared": declared_checksum},
                )

            expected = (received[-1] + 1) if received else 1
            if seq != expected:
                raise errors.PartOutOfOrder(
                    f"parts must be submitted in strict ascending order",
                    details={"part": seq, "expected_next": expected,
                             "received": received},
                )
            if seq > total:
                raise errors.PartOutOfOrder(
                    f"part {seq} exceeds total_parts={total}",
                    details={"part": seq, "expected_next": expected,
                             "total_parts": total},
                )

            # 在途字节计入配额：先判定再落盘，失败不占用也不留分段
            self._charge_inflight(namespace, session, len(data))

            dst = self.store.part_path(namespace, session_id, seq)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp")
            with tmp.open("wb") as f:
                f.write(data)
                f.flush()
            os.replace(tmp, dst)
            parts[str(seq)] = {"checksum": declared_checksum, "size": len(data)}
            self.store.save_session(namespace, session)
            self.store.append_audit(
                {"action": "part_accept", "namespace": namespace,
                 "session_id": session_id, "part": seq,
                 "size": len(data), "checksum": declared_checksum,
                 "received_count": len(parts),
                 "session_inflight": session["inflight_bytes"]})
            log.info("part_accept sid=%s part=%d/%d size=%d checksum=%s "
                     "session_inflight=%d",
                     session_id, seq, total, len(data), declared_checksum,
                     session["inflight_bytes"])
            return {"part": seq, "checksum": declared_checksum, "replayed": False,
                    "next_expected": seq + 1}

    # ================= 完成上传 =================
    def complete_session(self, namespace: str, session_id: str) -> CompleteResult:
        with self.store.lock():
            session = self._get_open_session(namespace, session_id)
            total = session["total_parts"]
            parts = session["parts"]
            missing = [i for i in range(1, total + 1) if str(i) not in parts]
            if missing:
                raise errors.PartsIncomplete(
                    "cannot complete: parts missing",
                    details={"missing": missing, "total_parts": total},
                )

            nsrec = self.store.get_namespace(namespace)
            assert nsrec is not None
            object_name = session["object_name"]

            # 乐观并发：在同一把锁内判定，胜负确定
            meta = self.store.get_object_meta(namespace, object_name)
            live_meta = meta is not None and not meta.get("delete_marker", False)
            current_version = (
                meta["current_version"]
                if live_meta
                else 0
            )
            expected = session.get("expected_version")
            if expected is not None and expected != current_version:
                decision = (
                    "expected_version_mismatch:"
                    f"expected={expected},actual_current={current_version}"
                )
                self.store.append_audit(
                    {"action": "complete_reject", "namespace": namespace,
                     "session_id": session_id, "object": object_name,
                     "reason": "version_conflict", "decision": decision})
                log.warning("complete_reject sid=%s object=%s %s", session_id,
                            object_name, decision)
                raise errors.VersionConflict(
                    "object changed since session creation",
                    details={"expected_version": expected,
                             "actual_version": current_version},
                )

            # 顺序组装到临时文件
            tmp_blob = self.store.new_temp_blob(namespace)
            hasher = hashlib.sha256()
            total_size = 0
            try:
                with tmp_blob.open("wb") as out:
                    for i in range(1, total + 1):
                        ppath = self.store.part_path(namespace, session_id, i)
                        with ppath.open("rb") as src:
                            shutil.copyfileobj(src, out, length=1024 * 1024)
                        pmeta = parts[str(i)]
                        hasher.update(ppath.read_bytes())
                        total_size += pmeta["size"]
                checksum, size = _sha256_file(tmp_blob)
                if checksum != session["final_checksum"]:
                    raise errors.ObjectChecksumMismatch(
                        "assembled object checksum differs from declared final_checksum",
                        details={"declared": session["final_checksum"],
                                 "actual": checksum},
                    )
                if size != total_size:  # 防御性校验
                    raise errors.ObjectChecksumMismatch(
                        "assembled size differs from sum of part sizes",
                        details={"assembled": size, "sum_parts": total_size},
                    )

                # 配额不变式：所有已接收分段在 upload_part 时已计入
                # inflight_bytes 并通过 used+inflight<=quota 判定，因此此处
                # 无需再次检查——complete 只是把在途占用转为已用占用。

                # 提交：blob 就位 -> 写元数据 -> 更新会话/配额，全部在锁内
                blob = self.store.blob_path(namespace, checksum)
                if not blob.exists():
                    blob.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(tmp_blob, blob)
                else:
                    tmp_blob.unlink(missing_ok=True)

                now = iso(utcnow())
                if meta is None:
                    meta = {
                        "namespace": namespace, "object_name": object_name,
                        "current_version": 0, "delete_marker": False,
                        "versions": [],
                    }
                # 版本号单调递增：覆盖与删除后重建都在历史链之上 +1
                version_no = meta["current_version"] + 1
                new_version = {
                    "version": version_no,
                    "size": total_size,
                    "checksum": checksum,
                    "content_type": session["content_type"],
                    "metadata": session["metadata"],
                    "created_at": now,
                    "session_id": session_id,
                    "replaced_version": current_version if live_meta else None,
                }
                meta["versions"].append(new_version)
                meta["current_version"] = version_no
                meta["delete_marker"] = False

                # 历史版本保留策略：超出部分自动清理（只动内存，统一落盘）
                pruned, freed = self._apply_retention(namespace, meta, nsrec)

                # 先持久化元数据（未完成的对象永远不会出现：blob 已就位才写）
                self.store.put_object_meta(namespace, meta)
                # 元数据落盘后再回收被清理版本的 blob（共享内容不受影响）
                blobs_removed = self._reclaim_pruned_blobs(namespace, pruned)
                # 在途 -> 已用：used += 新版本大小 - 清理释放；inflight 释放本会话
                self._release_session_inflight(namespace, session)
                used_after = nsrec.get("used_bytes", 0) + total_size - freed
                self.store.update_namespace(namespace, used_bytes=used_after)

                session["status"] = "completed"
                session["completed_version"] = version_no
                self.store.save_session(namespace, session)
                # 保留 session.json 作为完成留痕，只回收分段数据
                self.store.remove_session_parts(namespace, session_id)

                outcome = "replaced" if live_meta else "created"
                decision = (f"serialized_commit:{outcome},new_version={version_no},"
                            f"previous_version={current_version or 'none'},"
                            f"lock=store_rlock,"
                            f"pruned={[v['version'] for v in pruned]},"
                            f"used_after={used_after}")
                self.store.append_audit(
                    {"action": "session_complete", "namespace": namespace,
                     "session_id": session_id, "object": object_name,
                     "version": version_no, "outcome": outcome,
                     "checksum": checksum, "size": total_size,
                     "pruned_versions": [v["version"] for v in pruned],
                     "blobs_reclaimed": blobs_removed,
                     "decision": decision})
                log.info("session_complete sid=%s object=%s version=%d outcome=%s %s",
                         session_id, object_name, version_no, outcome, decision)
                return CompleteResult(
                    namespace=namespace, object_name=object_name,
                    version=version_no,
                    replaced_version=current_version if live_meta else None,
                    checksum=checksum, size=total_size, outcome=outcome,
                    decision=decision)
            except errors.ServiceError:
                tmp_blob.unlink(missing_ok=True)
                raise
            except Exception:
                tmp_blob.unlink(missing_ok=True)
                raise

    # ================= 读取 / 列表 / 删除 =================
    def _require_object(self, namespace: str, object_name: str) -> dict[str, Any]:
        meta = self.store.get_object_meta(namespace, object_name)
        if meta is None:
            raise errors.ObjectNotFound(
                f"object {object_name!r} not found",
                details={"namespace": namespace, "object": object_name})
        return meta

    def get_object(self, namespace: str, object_name: str,
                   version: int | None = None) -> tuple[dict[str, Any], Path, int]:
        meta = self._require_object(namespace, object_name)
        if meta.get("delete_marker") and version is None:
            raise errors.ObjectNotFound(
                f"object {object_name!r} has been deleted",
                details={"namespace": namespace, "object": object_name,
                         "delete_marker": True})
        vno = version if version is not None else meta["current_version"]
        vmeta = next((v for v in meta["versions"] if v["version"] == vno), None)
        if vmeta is None:
            raise errors.VersionNotFound(
                f"version {vno} not found",
                details={"object": object_name, "requested_version": vno,
                         "available": [v["version"] for v in meta["versions"]]})
        path = self.store.blob_path(namespace, vmeta["checksum"])
        if not path.exists():
            # 不应发生：blob 先于元数据存在；给出明确错误而非 500
            raise errors.ServiceError(
                "object data missing on disk (possible corruption)",
                details={"checksum": vmeta["checksum"]})
        return vmeta, path, vno

    def stat_object(self, namespace: str, object_name: str) -> ObjectInfo:
        meta = self._require_object(namespace, object_name)
        return self._object_info(meta)

    def list_objects(self, namespace: str) -> list[ObjectInfo]:
        return [self._object_info(m) for m in self.store.list_object_metas(namespace)]

    @staticmethod
    def _object_info(meta: dict[str, Any]) -> ObjectInfo:
        return ObjectInfo(
            namespace=meta["namespace"], object_name=meta["object_name"],
            current_version=meta["current_version"],
            delete_marker=meta.get("delete_marker", False),
            versions=[VersionInfo(**{k: v for k, v in v.items()
                                     if k != "replaced_version"})
                      for v in meta["versions"]],
        )

    @staticmethod
    def _session_info(s: dict[str, Any]) -> SessionInfo:
        return SessionInfo(
            session_id=s["session_id"], namespace=s["namespace"],
            object_name=s["object_name"], total_parts=s["total_parts"],
            received_parts=sorted(int(k) for k in s.get("parts", {})),
            received_bytes=sum(int(p.get("size", 0))
                               for p in s.get("parts", {}).values()),
            status=s["status"], expires_at=s["expires_at"])

    def delete_object(self, namespace: str, object_name: str) -> dict[str, Any]:
        """逻辑删除：仅置删除标记。

        全部版本（含当前版本）保留且可按版本读取，因此不回收任何 blob、
        不释放配额——空间只能通过删除历史版本/保留策略清理来释放，
        保证"版本信息还在、内容必然可取"。
        """
        with self.store.lock():
            meta = self.store.get_object_meta(namespace, object_name)
            if meta is None:
                raise errors.ObjectNotFound(f"object {object_name!r} not found")
            meta["delete_marker"] = True
            self.store.put_object_meta(namespace, meta)
            self.store.append_audit(
                {"action": "object_delete", "namespace": namespace,
                 "object": object_name, "current_version": meta["current_version"],
                 "decision": "delete_marker_only,history_and_quota_retained"})
            log.info("object_delete ns=%s object=%s version=%d "
                     "history_retained=%d",
                     namespace, object_name, meta["current_version"],
                     len(meta["versions"]))
            return {"deleted": True, "object": object_name,
                    "current_version": meta["current_version"],
                    "history_retained": len(meta["versions"]),
                    "bytes_freed": 0}

    # ================= 历史版本：保留策略 / 手动清理 / 按版本恢复 =================
    def _apply_retention(self, namespace: str, meta: dict[str, Any],
                         nsrec: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        """按命名空间保留策略裁剪历史版本（保留当前版本 + 最新 N 个历史）。

        只改内存中的 meta，调用方负责落盘与 blob 回收。
        返回 (被裁掉的版本列表, 释放的逻辑字节数)。
        """
        max_hist = nsrec.get("max_history_versions")
        if max_hist is None:
            return [], 0
        current = meta["current_version"]
        history = sorted((v for v in meta["versions"]
                          if v["version"] != current),
                         key=lambda v: v["version"])
        excess = len(history) - max_hist
        if excess <= 0:
            return [], 0
        pruned = history[:excess]
        pruned_nos = {v["version"] for v in pruned}
        meta["versions"] = [v for v in meta["versions"]
                            if v["version"] not in pruned_nos]
        return pruned, sum(int(v["size"]) for v in pruned)

    def _reclaim_pruned_blobs(self, namespace: str,
                              pruned: list[dict[str, Any]]) -> int:
        """裁剪的元数据落盘后再回收 blob：仅当全命名空间无任何保留版本
        引用该内容时才删除（共享内容不会被误删）。返回实际删除的 blob 数。"""
        removed = 0
        for v in pruned:
            if self.store.remove_blob_if_unreferferenced(namespace, v["checksum"]):
                removed += 1
        return removed

    def delete_version(self, namespace: str, object_name: str,
                       version: int) -> dict[str, Any]:
        """手动删除一个历史版本；当前生效版本（对象未删除时）不允许删。"""
        with self.store.lock():
            meta = self.store.get_object_meta(namespace, object_name)
            if meta is None:
                raise errors.ObjectNotFound(
                    f"object {object_name!r} not found",
                    details={"namespace": namespace, "object": object_name})
            vmeta = next((v for v in meta["versions"]
                          if v["version"] == version), None)
            if vmeta is None:
                raise errors.VersionNotFound(
                    f"version {version} not found",
                    details={"object": object_name, "requested_version": version,
                             "available": [v["version"]
                                           for v in meta["versions"]]})
            if (version == meta["current_version"]
                    and not meta.get("delete_marker", False)):
                self.store.append_audit(
                    {"action": "version_delete_reject", "namespace": namespace,
                     "object": object_name, "version": version,
                     "decision": "current_version_in_use"})
                raise errors.VersionInUse(
                    "cannot delete the current live version; "
                    "delete the object or overwrite it first",
                    details={"object": object_name, "version": version})
            meta["versions"] = [v for v in meta["versions"]
                                if v["version"] != version]
            # 版本号只增不减：current_version 保持不变，空出的序号永不复用
            self.store.put_object_meta(namespace, meta)
            blobs_removed = self._reclaim_pruned_blobs(namespace, [vmeta])
            freed = int(vmeta["size"])
            nsrec = self.store.get_namespace(namespace)
            used_after = max(0, nsrec.get("used_bytes", 0) - freed)
            self.store.update_namespace(namespace, used_bytes=used_after)
            decision = (f"version_pruned:manual,blob_reclaimed={blobs_removed},"
                        f"used_after={used_after}")
            self.store.append_audit(
                {"action": "version_delete", "namespace": namespace,
                 "object": object_name, "version": version,
                 "size": freed, "decision": decision})
            log.info("version_delete ns=%s object=%s v=%d freed=%d %s",
                     namespace, object_name, version, freed, decision)
            return {"object": object_name, "deleted_version": version,
                    "bytes_freed": freed, "blob_reclaimed": blobs_removed,
                    "remaining_versions": [v["version"]
                                           for v in meta["versions"]]}

    def restore_object(self, namespace: str, object_name: str,
                       body: RestoreRequest) -> RestoreResult:
        """把指定历史版本恢复为新的当前版本。

        内容/类型/元信息复制自源版本；版本号在历史链之上继续递增；
        已删除对象恢复后删除标记清除。三种目标状态可区分：
        对象不存在 -> object_not_found；版本不存在 -> version_not_found；
        对象已删除 -> 成功且 was_delete_marked=True / outcome=restored_from_deleted。
        """
        with self.store.lock():
            meta = self.store.get_object_meta(namespace, object_name)
            if meta is None:
                raise errors.ObjectNotFound(
                    f"object {object_name!r} not found",
                    details={"namespace": namespace, "object": object_name})
            src = next((v for v in meta["versions"]
                        if v["version"] == body.version), None)
            if src is None:
                raise errors.VersionNotFound(
                    f"version {body.version} not found",
                    details={"object": object_name,
                             "requested_version": body.version,
                             "available": [v["version"]
                                           for v in meta["versions"]]})
            was_deleted = bool(meta.get("delete_marker", False))
            current_version = 0 if was_deleted else meta["current_version"]
            expected = body.expected_version
            if expected is not None and expected != current_version:
                decision = (f"expected_version_mismatch:expected={expected},"
                            f"actual_current={current_version}")
                self.store.append_audit(
                    {"action": "restore_reject", "namespace": namespace,
                     "object": object_name, "version": body.version,
                     "reason": "version_conflict", "decision": decision})
                raise errors.VersionConflict(
                    "object changed since restore was requested",
                    details={"expected_version": expected,
                             "actual_version": current_version})
            blob = self.store.blob_path(namespace, src["checksum"])
            if not blob.exists():
                raise errors.ServiceError(
                    "source version data missing on disk (possible corruption)",
                    details={"checksum": src["checksum"]})
            size = int(src["size"])
            # 恢复产生的新版本同样占配额（在锁内判定）
            nsrec = self.store.get_namespace(namespace)
            assert nsrec is not None
            quota = nsrec.get("quota_bytes", 0) or 0
            used = nsrec.get("used_bytes", 0)
            inflight = nsrec.get("inflight_bytes", 0)
            projected = used + inflight + size
            if quota and projected > quota:
                decision = (f"quota:used={used},inflight={inflight},"
                            f"restore_size={size},projected={projected},"
                            f"quota={quota}")
                self.store.append_audit(
                    {"action": "restore_reject", "namespace": namespace,
                     "object": object_name, "version": body.version,
                     "reason": "quota_exceeded", "decision": decision})
                raise errors.QuotaExceeded(
                    "restore would exceed namespace quota",
                    details={"used": used, "inflight": inflight,
                             "quota": quota, "projected": projected,
                             "incoming_size": size})
            new_no = meta["current_version"] + 1
            new_version = {
                "version": new_no,
                "size": size,
                "checksum": src["checksum"],
                "content_type": src["content_type"],
                "metadata": dict(src.get("metadata", {})),
                "created_at": iso(utcnow()),
                "session_id": None,
                "restored_from": body.version,
            }
            meta["versions"].append(new_version)
            meta["current_version"] = new_no
            meta["delete_marker"] = False
            pruned, freed = self._apply_retention(namespace, meta, nsrec)
            self.store.put_object_meta(namespace, meta)
            blobs_removed = self._reclaim_pruned_blobs(namespace, pruned)
            used_after = used + size - freed
            self.store.update_namespace(namespace, used_bytes=used_after)
            outcome = "restored_from_deleted" if was_deleted else "restored"
            decision = (f"serialized_restore:src=v{body.version},"
                        f"new_version={new_no},was_deleted={was_deleted},"
                        f"lock=store_rlock,pruned={[v['version'] for v in pruned]},"
                        f"used_after={used_after}")
            self.store.append_audit(
                {"action": "object_restore", "namespace": namespace,
                 "object": object_name, "restored_from": body.version,
                 "version": new_no, "outcome": outcome,
                 "pruned_versions": [v["version"] for v in pruned],
                 "decision": decision})
            log.info("object_restore ns=%s object=%s v%d->v%d outcome=%s %s",
                     namespace, object_name, body.version, new_no, outcome,
                     decision)
            return RestoreResult(
                namespace=namespace, object_name=object_name,
                version=new_no, restored_from=body.version,
                was_delete_marked=was_deleted,
                checksum=src["checksum"], size=size,
                outcome=outcome, decision=decision)

    # ================= 回收 =================
    def purge_expired_sessions(self, namespace: str) -> dict[str, Any]:
        """回收过期未完成会话：状态置 expired、删除分段并归还在途配额。"""
        with self.store.lock():
            now = utcnow()
            purged = []
            for s in self.store.all_sessions(namespace):
                if s["status"] in ("completed", "aborted"):
                    continue
                if parse_iso(s["expires_at"]) <= now:
                    s["status"] = "expired"
                    released = self._release_session_inflight(namespace, s)
                    self.store.save_session(namespace, s)
                    self.store.remove_session_parts(namespace, s["session_id"])
                    purged.append(s["session_id"])
                    self.store.append_audit(
                        {"action": "session_purge", "namespace": namespace,
                         "session_id": s["session_id"],
                         "decision": f"ttl_expired,parts_reclaimed,"
                                     f"inflight_released={released}"})
                    log.info("session_purge sid=%s ns=%s inflight_released=%d",
                             s["session_id"], namespace, released)
            return {"purged": purged, "count": len(purged)}

