"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---- 命名空间 ----
class NamespaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    quota_bytes: int = Field(default=0, ge=0, description="0 表示不限制")
    tokens: list[str] = Field(default_factory=list, description="授权令牌列表")
    max_history_versions: int | None = Field(
        default=None, ge=0,
        description="每个对象最多保留的历史（非当前）版本数；"
        "null 表示不限制。超出的最旧历史版本在新提交时自动清理。",
    )


class NamespaceInfo(BaseModel):
    name: str
    quota_bytes: int
    used_bytes: int
    inflight_bytes: int = 0
    max_history_versions: int | None = None
    tokens: list[str]


# ---- 上传会话 ----
class SessionCreate(BaseModel):
    object_name: str = Field(min_length=1, max_length=1024)
    total_parts: int = Field(ge=1)
    final_checksum: str = Field(description="完整对象的 sha256，hex")
    content_type: str = "application/octet-stream"
    metadata: dict[str, str] = Field(default_factory=dict)
    expected_version: int | None = Field(
        default=None,
        description="乐观并发：仅当当前最新版本号等于此值才允许覆盖；"
        "对象不存在时传 0；省略表示不检查。",
    )


class SessionInfo(BaseModel):
    session_id: str
    namespace: str
    object_name: str
    total_parts: int
    received_parts: list[int]
    received_bytes: int = 0
    status: str  # open | completed | aborted | expired
    expires_at: str


# ---- 版本 / 对象 ----
class VersionInfo(BaseModel):
    version: int
    size: int
    checksum: str
    content_type: str
    metadata: dict[str, str]
    created_at: str
    session_id: str | None = None
    restored_from: int | None = None


class ObjectInfo(BaseModel):
    namespace: str
    object_name: str
    current_version: int
    delete_marker: bool
    versions: list[VersionInfo]


class CompleteResult(BaseModel):
    namespace: str
    object_name: str
    version: int
    replaced_version: int | None
    checksum: str
    size: int
    outcome: str  # created | replaced
    decision: str  # 判定依据（用于日志/可解释性）


class ListResult(BaseModel):
    namespace: str
    objects: list[ObjectInfo]


# ---- 按版本恢复 ----
class RestoreRequest(BaseModel):
    version: int = Field(ge=1, description="要恢复的历史版本号")
    expected_version: int | None = Field(
        default=None,
        description="乐观并发：仅当当前最新版本号等于此值才允许恢复；"
        "已删除对象按 0 计；省略表示不检查。",
    )


class RestoreResult(BaseModel):
    namespace: str
    object_name: str
    version: int  # 恢复产生的新版本号
    restored_from: int  # 被恢复的源版本号
    was_delete_marked: bool  # 恢复前对象是否处于已删除状态
    checksum: str
    size: int
    outcome: str  # restored | restored_from_deleted
    decision: str  # 判定依据
