"""错误分类：所有业务错误都带有可区分的 reason 码与 HTTP 状态。"""

from __future__ import annotations


class StorageError(Exception):
    """业务错误基类。reason 用于客户端区分错误原因。"""

    status_code: int = 400
    reason: str = "storage_error"

    def __init__(self, message: str = "", *, detail: dict | None = None):
        super().__init__(message or self.reason)
        self.message = message or self.reason
        self.detail = detail or {}


# --- 命名空间相关 ---
class NamespaceNotFoundError(StorageError):
    status_code = 404
    reason = "namespace_not_found"


# --- 上传会话 / 分段相关 ---
class SessionNotFoundError(StorageError):
    status_code = 404
    reason = "upload_session_not_found"


class SessionStateError(StorageError):
    status_code = 409
    reason = "upload_session_invalid_state"


class SessionExpiredError(StorageError):
    status_code = 410
    reason = "upload_session_expired"


class PartNumberError(StorageError):
    status_code = 400
    reason = "invalid_part_number"


class PartSizeError(StorageError):
    status_code = 400
    reason = "invalid_part_size"


class PartChecksumError(StorageError):
    status_code = 400
    reason = "part_checksum_mismatch"


class PartConflictError(StorageError):
    status_code = 409
    reason = "part_conflict"


class DigestMismatchError(StorageError):
    status_code = 422
    reason = "digest_mismatch"


class MissingPartsError(StorageError):
    status_code = 409
    reason = "missing_parts"


# --- 对象 / 版本相关 ---
class ObjectNotFoundError(StorageError):
    status_code = 404
    reason = "object_not_found"


class VersionNotFoundError(StorageError):
    status_code = 404
    reason = "version_not_found"


class CommitConflictError(StorageError):
    status_code = 409
    reason = "commit_conflict"


# --- 授权 / 配额相关 ---
class UnauthorizedError(StorageError):
    status_code = 401
    reason = "unauthorized"


class ForbiddenError(StorageError):
    status_code = 403
    reason = "forbidden"


class QuotaExceededError(StorageError):
    status_code = 413
    reason = "quota_exceeded"
