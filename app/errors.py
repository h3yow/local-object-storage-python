"""统一错误分类与可区分错误码。

所有业务错误都映射为稳定的 ``error_code`` 字符串，客户端可据此区分失败原因，
而不是仅依赖 HTTP 状态码或解析自然语言消息。
"""
from __future__ import annotations


class ServiceError(Exception):
    """业务错误基类：携带稳定 error_code 与 HTTP 状态码。"""

    status_code: int = 400
    error_code: str = "bad_request"

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message or self.error_code)
        self.message = message or self.error_code
        self.details = details or {}

    def to_body(self) -> dict:
        return {
            "error": {
                "code": self.error_code,
                "message": self.message,
                "details": self.details,
            }
        }


# ---- 认证与授权 (401 / 403) ----
class MissingCredentials(ServiceError):
    status_code = 401
    error_code = "missing_credentials"


class InvalidToken(ServiceError):
    status_code = 401
    error_code = "invalid_token"


class NamespaceAccessDenied(ServiceError):
    status_code = 403
    error_code = "namespace_access_denied"


# ---- 资源不存在 / 已失效 ----
class NamespaceNotFound(ServiceError):
    status_code = 404
    error_code = "namespace_not_found"


class ObjectNotFound(ServiceError):
    status_code = 404
    error_code = "object_not_found"


class VersionNotFound(ServiceError):
    status_code = 404
    error_code = "version_not_found"


class SessionNotFound(ServiceError):
    status_code = 404
    error_code = "upload_session_not_found"


class SessionExpired(ServiceError):
    """会话存在但已超过 TTL，属于可区分的"已失效"错误 (410 Gone)。"""

    status_code = 410
    error_code = "upload_session_expired"


class SessionClosed(ServiceError):
    """会话已完成或已中止，不能再提交分段 (409)。"""

    status_code = 409
    error_code = "upload_session_closed"


# ---- 分段上传校验 ----
class PartOutOfOrder(ServiceError):
    """分段序号非法：必须严格按 1,2,3... 顺序首次提交。"""

    status_code = 409
    error_code = "part_out_of_order"


class PartChecksumMismatch(ServiceError):
    """分段内容与声明的 sha256 不一致 (422)。"""

    status_code = 422
    error_code = "part_checksum_mismatch"


class PartConflict(ServiceError):
    """同序号分段重复提交但摘要不同（与幂等重放区分开）。"""

    status_code = 409
    error_code = "part_conflict"


class PartsIncomplete(ServiceError):
    """完成请求时分段缺失或 total_parts 不一致。"""

    status_code = 409
    error_code = "parts_incomplete"


class ObjectChecksumMismatch(ServiceError):
    """组装后对象摘要与客户端声明摘要不一致 (422)。"""

    status_code = 422
    error_code = "object_checksum_mismatch"


# ---- 版本管理 ----
class VersionInUse(ServiceError):
    """试图直接删除当前生效版本（未被删除标记覆盖的对象）(409)。"""

    status_code = 409
    error_code = "version_in_use"


# ---- 并发 / 配额 ----
class VersionConflict(ServiceError):
    """乐观并发：客户端期望的当前版本与实际不符 (409)。"""

    status_code = 409
    error_code = "version_conflict"


class QuotaExceeded(ServiceError):
    """写入将突破命名空间配额 (413)。"""

    status_code = 413
    error_code = "quota_exceeded"


class ObjectLocked(ServiceError):
    """对象上存在进行中的覆盖判定（理论上由锁串行化，兜底用）。"""

    status_code = 409
    error_code = "object_locked"
