"""核心服务：上传会话、版本提交、配额与授权（骨架：空实现）。"""

from __future__ import annotations

import threading

from .models import Namespace, ObjectVersion, UploadSession
from .storage import LocalStorage


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

    # --- 命名空间与授权 ---
    def create_namespace(self, name: str, quota_bytes: int) -> Namespace:
        raise NotImplementedError

    def grant(self, namespace: str, token: str, *, read: bool, write: bool) -> None:
        raise NotImplementedError

    def authorize(self, namespace: str, token: str | None, action: str) -> None:
        raise NotImplementedError

    # --- 分段上传 ---
    def create_upload(self, namespace: str, key: str, *, expected_parts: int,
                      final_sha256: str, metadata: dict | None = None) -> UploadSession:
        raise NotImplementedError

    def upload_part(self, session_id: str, number: int, data: bytes, part_sha256: str) -> None:
        raise NotImplementedError

    def complete_upload(self, session_id: str) -> ObjectVersion:
        raise NotImplementedError

    def abort_upload(self, session_id: str) -> None:
        raise NotImplementedError

    # --- 读取 / 删除 ---
    def get_object(self, namespace: str, key: str, version: int | None = None) -> tuple[ObjectVersion, bytes]:
        raise NotImplementedError

    def list_versions(self, namespace: str, key: str) -> list[ObjectVersion]:
        raise NotImplementedError

    def delete_object(self, namespace: str, key: str) -> ObjectVersion:
        raise NotImplementedError

    # --- 回收 ---
    def reap_expired_sessions(self, now: float | None = None) -> list[str]:
        raise NotImplementedError
