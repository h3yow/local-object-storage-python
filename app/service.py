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

    # ================= 命名空间 / 授权 =================
    def create_namespace(self, body: NamespaceCreate) -> NamespaceInfo:
        with self.store.lock():
            if self.store.namespace_exists(body.name):
                raise errors.ServiceError(
                    f"namespace {body.name!r} already exists",
                )
            rec = self.store.create_namespace(body.name, body.quota_bytes, body.tokens)
            self.store.append_audit(
                {"action": "namespace_create", "namespace": body.name,
                 "quota_bytes": body.quota_bytes, "tokens": body.tokens}
            )
            log.info("namespace_create name=%s quota=%s tokens=%d",
                     body.name, body.quota_bytes, len(body.tokens))
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
            used_bytes=rec.get("used_bytes", 0), tokens=list(rec.get("tokens", [])),
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
            # 惰性过期：仅改状态，目录由回收统一清理。
            session["status"] = "expired"
            self.store.save_session(namespace, session)
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
                self.store.save_session(namespace, session)
        return self._session_info(session)

    def abort_session(self, namespace: str, session_id: str) -> SessionInfo:
        with self.store.lock():
            session = self.store.load_session(namespace, session_id)
            if session is None:
                raise errors.SessionNotFound(f"upload session {session_id!r} not found")
            if session["status"] in ("open", "expired"):
                session["status"] = "aborted"
                self.store.save_session(namespace, session)
                self.store.append_audit(
                    {"action": "session_abort", "namespace": namespace,
                     "session_id": session_id,
                     "decision": "aborted,parts_reclaimed"})
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
                 "received_count": len(parts)})
            log.info("part_accept sid=%s part=%d/%d size=%d checksum=%s",
                     session_id, seq, total, len(data), declared_checksum)
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

                # 配额判定（在锁内，先查旧版本占用再决定）
                quota = nsrec.get("quota_bytes", 0) or 0
                used = nsrec.get("used_bytes", 0)
                old_size = 0
                if live_meta:
                    old_size = next(
                        (v["size"] for v in meta["versions"]
                         if v["version"] == current_version), 0)
                projected = used + total_size - old_size
                if quota and projected > quota:
                    decision = (f"quota:used={used},new={total_size},"
                                f"replace_old={old_size},projected={projected},"
                                f"quota={quota}")
                    self.store.append_audit(
                        {"action": "complete_reject", "namespace": namespace,
                         "session_id": session_id, "object": object_name,
                         "reason": "quota_exceeded", "decision": decision})
                    log.warning("complete_reject sid=%s object=%s %s", session_id,
                                object_name, decision)
                    raise errors.QuotaExceeded(
                        "write would exceed namespace quota",
                        details={"used": used, "quota": quota,
                                 "projected": projected,
                                 "incoming_size": total_size},
                    )

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

                # 先持久化元数据（未完成的对象永远不会出现：blob 已就位才写）
                self.store.put_object_meta(namespace, meta)
                self.store.update_namespace(namespace, used_bytes=projected)

                session["status"] = "completed"
                session["completed_version"] = version_no
                self.store.save_session(namespace, session)
                # 保留 session.json 作为完成留痕，只回收分段数据
                self.store.remove_session_parts(namespace, session_id)

                outcome = "replaced" if live_meta else "created"
                decision = (f"serialized_commit:{outcome},new_version={version_no},"
                            f"previous_version={current_version or 'none'},"
                            f"lock=store_rlock,quota_after={projected}")
                self.store.append_audit(
                    {"action": "session_complete", "namespace": namespace,
                     "session_id": session_id, "object": object_name,
                     "version": version_no, "outcome": outcome,
                     "checksum": checksum, "size": total_size,
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
            status=s["status"], expires_at=s["expires_at"])

    def delete_object(self, namespace: str, object_name: str) -> dict[str, Any]:
        """逻辑删除：置删除标记并回收当前版本不再被引用的 blob，历史版本保留可回溯。"""
        with self.store.lock():
            meta = self.store.get_object_meta(namespace, object_name)
            if meta is None:
                raise errors.ObjectNotFound(f"object {object_name!r} not found")
            reclaimed = 0
            freed = 0
            # 当前版本 blob 若没有其他对象引用则回收
            cur = next((v for v in meta["versions"]
                        if v["version"] == meta["current_version"]), None)
            if cur is not None:
                before = self.store.blob_path(namespace, cur["checksum"]).exists()
                removed = self.store.remove_blob_if_unreferferenced(
                    namespace, cur["checksum"], exclude_object=object_name)
                if removed or (before and not self.store.blob_path(
                        namespace, cur["checksum"]).exists()):
                    reclaimed += 1
                    freed += cur["size"]
            meta["delete_marker"] = True
            self.store.put_object_meta(namespace, meta)
            nsrec = self.store.get_namespace(namespace)
            self.store.update_namespace(
                namespace, used_bytes=max(0, nsrec.get("used_bytes", 0) - freed))
            self.store.append_audit(
                {"action": "object_delete", "namespace": namespace,
                 "object": object_name, "current_version": meta["current_version"],
                 "blobs_reclaimed": reclaimed, "bytes_freed": freed,
                 "decision": "delete_marker_kept_history"})
            log.info("object_delete ns=%s object=%s version=%d reclaimed=%d freed=%d",
                     namespace, object_name, meta["current_version"], reclaimed, freed)
            return {"deleted": True, "object": object_name,
                    "current_version": meta["current_version"],
                    "history_retained": len(meta["versions"]),
                    "bytes_freed": freed}

    # ================= 回收 =================
    def purge_expired_sessions(self, namespace: str) -> dict[str, Any]:
        """回收过期未完成会话：状态置 expired 并删除其分段目录，不触碰对象数据。"""
        with self.store.lock():
            now = utcnow()
            purged = []
            for s in self.store.all_sessions(namespace):
                if s["status"] in ("completed", "aborted"):
                    continue
                if parse_iso(s["expires_at"]) <= now:
                    s["status"] = "expired"
                    self.store.save_session(namespace, s)
                    self.store.remove_session_parts(namespace, s["session_id"])
                    purged.append(s["session_id"])
                    self.store.append_audit(
                        {"action": "session_purge", "namespace": namespace,
                         "session_id": s["session_id"],
                         "decision": "ttl_expired,parts_reclaimed"})
                    log.info("session_purge sid=%s ns=%s", s["session_id"], namespace)
            return {"purged": purged, "count": len(purged)}

