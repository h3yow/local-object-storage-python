"""核心服务：上传会话、版本提交、配额与授权。

并发模型：所有状态变更都在同一把 RLock 下完成，判定与落账是原子的。
- 分段上传：重复提交同一分段（内容一致）幂等；内容不一致拒绝。
- 完成提交：基于 base_version 的乐观并发控制，先到者胜，后到者收到
  CommitConflictError 且能拿到当前获胜版本号，旧版本不会被无解释覆盖。
- 配额：已提交字节 + 在途分段字节 一起计入，并发写入不会突破配额。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid

from .errors import (
    CommitConflictError,
    DigestMismatchError,
    ForbiddenError,
    MissingPartsError,
    NamespaceNotFoundError,
    ObjectNotFoundError,
    PartChecksumError,
    PartConflictError,
    PartNumberError,
    QuotaExceededError,
    SessionExpiredError,
    SessionNotFoundError,
    SessionStateError,
    UnauthorizedError,
    VersionNotFoundError,
)
from .models import Namespace, ObjectVersion, PartInfo, SessionState, UploadSession
from .storage import LocalStorage

logger = logging.getLogger("objectstore")

_HEX64 = set("0123456789abcdef")


class ObjectService:
    def __init__(self, storage: LocalStorage, session_ttl: float = 3600.0):
        self.storage = storage
        self.session_ttl = session_ttl
        self._lock = threading.RLock()
        self._namespaces: dict[str, Namespace] = {}
        self._sessions: dict[str, UploadSession] = {}
        self._versions: dict[tuple[str, str], list[ObjectVersion]] = {}
        # namespace -> {token: {"read": bool, "write": bool}}
        self._grants: dict[str, dict[str, dict[str, bool]]] = {}
        # namespace -> 在途（未提交）分段字节数，与已提交字节一起计入配额
        self._inflight: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 命名空间与授权
    # ------------------------------------------------------------------
    def create_namespace(self, name: str, quota_bytes: int) -> Namespace:
        with self._lock:
            ns = self._namespaces.get(name)
            if ns is None:
                ns = Namespace(name=name, quota_bytes=quota_bytes)
                self._namespaces[name] = ns
                self._inflight.setdefault(name, 0)
                logger.info("namespace created: ns=%s quota=%d", name, quota_bytes)
            return ns

    def grant(self, namespace: str, token: str, *, read: bool, write: bool) -> None:
        with self._lock:
            self._require_namespace(namespace)
            self._grants.setdefault(namespace, {})[token] = {"read": read, "write": write}

    def authorize(self, namespace: str, token: str | None, action: str) -> None:
        """action 为 "read" 或 "write"。未认证与越权返回不同错误。"""
        with self._lock:
            self._require_namespace(namespace)
            grant = self._grants.get(namespace, {}).get(token or "")
            if grant is None:
                logger.info("authorize denied: ns=%s action=%s reason=unauthorized", namespace, action)
                raise UnauthorizedError(f"namespace {namespace!r}: 缺少有效凭证")
            if not grant.get(action, False):
                logger.info("authorize denied: ns=%s action=%s reason=forbidden", namespace, action)
                raise ForbiddenError(f"namespace {namespace!r}: 凭证无 {action} 权限")

    # ------------------------------------------------------------------
    # 分段上传
    # ------------------------------------------------------------------
    def create_upload(self, namespace: str, key: str, *, expected_parts: int,
                      final_sha256: str, metadata: dict | None = None) -> UploadSession:
        if expected_parts < 1:
            raise PartNumberError("expected_parts 必须 >= 1")
        digest = final_sha256.lower()
        if len(digest) != 64 or any(c not in _HEX64 for c in digest):
            raise DigestMismatchError("final_sha256 必须是 64 位十六进制小写摘要")
        with self._lock:
            self._require_namespace(namespace)
            versions = self._versions.get((namespace, key), [])
            base_version = versions[-1].version if versions else None
            now = time.time()
            session = UploadSession(
                session_id=uuid.uuid4().hex,
                namespace=namespace,
                key=key,
                state=SessionState.ACTIVE,
                expected_parts=expected_parts,
                final_sha256=digest,
                base_version=base_version,
                metadata=dict(metadata or {}),
                created_at=now,
                expires_at=now + self.session_ttl,
            )
            self._sessions[session.session_id] = session
            logger.info(
                "upload session created: session=%s ns=%s key=%s parts=%d base_version=%s",
                session.session_id, namespace, key, expected_parts, base_version,
            )
            return session

    def upload_part(self, session_id: str, number: int, data: bytes, part_sha256: str) -> None:
        with self._lock:
            session = self._require_active_session(session_id)
            if not 1 <= number <= session.expected_parts:
                raise PartNumberError(
                    f"分段号 {number} 超出范围 [1, {session.expected_parts}]",
                    detail={"expected_parts": session.expected_parts},
                )
            actual = hashlib.sha256(data).hexdigest()
            if actual != part_sha256.lower():
                logger.info(
                    "part rejected: session=%s part=%d reason=part_checksum_mismatch",
                    session_id, number,
                )
                raise PartChecksumError("分段内容与声明的摘要不一致")
            existing = session.parts.get(number)
            if existing is not None:
                if existing.sha256 == actual and existing.size == len(data):
                    logger.info(
                        "part duplicate accepted (idempotent): session=%s part=%d sha256=%s",
                        session_id, number, actual,
                    )
                    return  # 幂等：相同内容重复提交直接成功
                logger.info(
                    "part rejected: session=%s part=%d reason=part_conflict existing=%s new=%s",
                    session_id, number, existing.sha256, actual,
                )
                raise PartConflictError(
                    f"分段 {number} 已存在且内容不同",
                    detail={"existing_sha256": existing.sha256, "new_sha256": actual},
                )
            self._reserve_quota(session.namespace, len(data), session_id=session_id, part=number)
            path = self.storage.write_part(session.namespace, session_id, number, data)
            session.parts[number] = PartInfo(number=number, size=len(data), sha256=actual, path=str(path))
            self._inflight[session.namespace] += len(data)
            logger.info(
                "part stored: session=%s part=%d size=%d sha256=%s",
                session_id, number, len(data), actual,
            )

    def complete_upload(self, session_id: str) -> ObjectVersion:
        with self._lock:
            session = self._require_active_session(session_id)
            missing = [n for n in range(1, session.expected_parts + 1) if n not in session.parts]
            if missing:
                raise MissingPartsError("尚有分段未上传", detail={"missing_parts": missing})
            part_paths = [self.storage.part_path(session.namespace, session_id, n)
                          for n in range(1, session.expected_parts + 1)]
            total = sum(session.parts[n].size for n in range(1, session.expected_parts + 1))

            # 并发胜负判定：基于创建会话时看到的版本，先到者胜。
            versions = self._versions.setdefault((session.namespace, session.key), [])
            current = versions[-1].version if versions else None
            if current != session.base_version:
                self._abandon(session, SessionState.ABORTED)
                logger.info(
                    "commit rejected: session=%s key=%s reason=commit_conflict "
                    "base_version=%s current_version=%s",
                    session_id, session.key, session.base_version, current,
                )
                raise CommitConflictError(
                    "对象在会话期间已被其他提交更新",
                    detail={"base_version": session.base_version, "current_version": current},
                )

            # 摘要校验失败：会话作废并清理，避免悬挂数据。
            assembled = hashlib.sha256()
            for p in part_paths:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        assembled.update(chunk)
            if assembled.hexdigest() != session.final_sha256:
                self._abandon(session, SessionState.ABORTED)
                logger.info(
                    "commit rejected: session=%s key=%s reason=digest_mismatch declared=%s actual=%s",
                    session_id, session.key, session.final_sha256, assembled.hexdigest(),
                )
                raise DigestMismatchError(
                    "合并后的内容与声明的摘要不一致",
                    detail={"declared": session.final_sha256, "actual": assembled.hexdigest()},
                )

            version_no = (current or 0) + 1
            path = self.storage.commit_parts(session.namespace, session.key, version_no, part_paths)
            version = ObjectVersion(
                version=version_no,
                namespace=session.namespace,
                key=session.key,
                size=total,
                sha256=session.final_sha256,
                metadata=dict(session.metadata),
                path=str(path),
                created_at=time.time(),
                session_id=session_id,
            )
            versions.append(version)
            ns = self._namespaces[session.namespace]
            ns.used_bytes += total
            self._inflight[session.namespace] -= total
            session.state = SessionState.COMPLETED
            self.storage.delete_upload(session.namespace, session_id)
            logger.info(
                "commit success: session=%s ns=%s key=%s version=%d size=%d sha256=%s used=%d/%d",
                session_id, session.namespace, session.key, version_no, total,
                session.final_sha256, ns.used_bytes, ns.quota_bytes,
            )
            return version

    def abort_upload(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(f"上传会话 {session_id} 不存在")
            if session.state is not SessionState.ACTIVE:
                raise SessionStateError(f"会话已处于 {session.state.value} 状态，无法中止")
            self._abandon(session, SessionState.ABORTED)
            logger.info("upload aborted: session=%s", session_id)

    # ------------------------------------------------------------------
    # 读取 / 删除
    # ------------------------------------------------------------------
    def get_object(self, namespace: str, key: str, version: int | None = None) -> tuple[ObjectVersion, bytes]:
        with self._lock:
            self._require_namespace(namespace)
            versions = self._versions.get((namespace, key), [])
            if not versions:
                raise ObjectNotFoundError(f"对象 {namespace}/{key} 不存在")
            if version is None:
                target = versions[-1]
            else:
                match = [v for v in versions if v.version == version]
                if not match:
                    raise VersionNotFoundError(
                        f"对象 {namespace}/{key} 没有版本 {version}",
                        detail={"available_versions": [v.version for v in versions]},
                    )
                target = match[0]
            if target.deleted:
                raise ObjectNotFoundError(
                    f"对象 {namespace}/{key} 版本 {target.version} 是删除标记",
                    detail={"deleted_version": target.version},
                )
            return target, self.storage.read_version(namespace, key, target.version)

    def list_versions(self, namespace: str, key: str) -> list[ObjectVersion]:
        with self._lock:
            self._require_namespace(namespace)
            return list(self._versions.get((namespace, key), []))

    def delete_object(self, namespace: str, key: str) -> ObjectVersion:
        """写入删除标记（tombstone）版本，历史版本保留可回溯。"""
        with self._lock:
            self._require_namespace(namespace)
            versions = self._versions.get((namespace, key), [])
            if not versions or versions[-1].deleted:
                raise ObjectNotFoundError(f"对象 {namespace}/{key} 不存在或已删除")
            tombstone = ObjectVersion(
                version=versions[-1].version + 1,
                namespace=namespace,
                key=key,
                size=0,
                sha256="",
                metadata={},
                path="",
                created_at=time.time(),
                session_id="",
                deleted=True,
            )
            versions.append(tombstone)
            logger.info(
                "object deleted: ns=%s key=%s tombstone_version=%d retained_versions=%s",
                namespace, key, tombstone.version, [v.version for v in versions],
            )
            return tombstone

    # ------------------------------------------------------------------
    # 回收
    # ------------------------------------------------------------------
    def reap_expired_sessions(self, now: float | None = None) -> list[str]:
        """回收过期会话：释放在途配额、删除分段数据，不留下悬挂文件。"""
        now = time.time() if now is None else now
        reaped: list[str] = []
        with self._lock:
            for session in self._sessions.values():
                if session.state is SessionState.ACTIVE and session.expires_at <= now:
                    self._abandon(session, SessionState.EXPIRED)
                    reaped.append(session.session_id)
                    logger.info(
                        "session reaped: session=%s ns=%s key=%s uploaded_parts=%d",
                        session.session_id, session.namespace, session.key, len(session.parts),
                    )
        return reaped

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _require_namespace(self, namespace: str) -> Namespace:
        ns = self._namespaces.get(namespace)
        if ns is None:
            raise NamespaceNotFoundError(f"命名空间 {namespace!r} 不存在")
        return ns

    def _require_active_session(self, session_id: str) -> UploadSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"上传会话 {session_id} 不存在")
        if session.state is SessionState.ACTIVE and session.expires_at <= time.time():
            self._abandon(session, SessionState.EXPIRED)
        if session.state is SessionState.EXPIRED:
            raise SessionExpiredError(f"上传会话 {session_id} 已过期")
        if session.state is not SessionState.ACTIVE:
            raise SessionStateError(f"会话已处于 {session.state.value} 状态")
        return session

    def _reserve_quota(self, namespace: str, size: int, **ctx) -> None:
        ns = self._namespaces[namespace]
        projected = ns.used_bytes + self._inflight[namespace] + size
        if projected > ns.quota_bytes:
            logger.info(
                "quota rejected: ns=%s used=%d inflight=%d request=%d quota=%d ctx=%s",
                namespace, ns.used_bytes, self._inflight[namespace], size, ns.quota_bytes, ctx,
            )
            raise QuotaExceededError(
                f"命名空间 {namespace!r} 配额不足",
                detail={
                    "used_bytes": ns.used_bytes,
                    "inflight_bytes": self._inflight[namespace],
                    "requested_bytes": size,
                    "quota_bytes": ns.quota_bytes,
                },
            )

    def _abandon(self, session: UploadSession, state: SessionState) -> None:
        """会话终止的统一出口：释放在途配额并清理分段文件。"""
        uploaded = sum(p.size for p in session.parts.values())
        self._inflight[session.namespace] -= uploaded
        session.state = state
        session.parts.clear()
        self.storage.delete_upload(session.namespace, session.session_id)
