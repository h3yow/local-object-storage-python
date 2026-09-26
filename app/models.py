"""领域模型：命名空间、上传会话、对象版本（仅类型定义，骨架阶段）。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class SessionState(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABORTED = "aborted"
    EXPIRED = "expired"


@dataclass
class PartInfo:
    number: int
    size: int
    sha256: str
    path: str


@dataclass
class UploadSession:
    session_id: str
    namespace: str
    key: str
    state: SessionState
    expected_parts: int
    final_sha256: str
    base_version: int | None  # 创建会话时看到的最新版本，用于并发胜负判定
    metadata: dict
    created_at: float
    expires_at: float
    parts: dict[int, PartInfo] = field(default_factory=dict)


@dataclass
class ObjectVersion:
    version: int
    namespace: str
    key: str
    size: int
    sha256: str
    metadata: dict
    path: str
    created_at: float
    session_id: str
    deleted: bool = False  # 删除标记（tombstone）


@dataclass
class Namespace:
    name: str
    quota_bytes: int
    used_bytes: int = 0
