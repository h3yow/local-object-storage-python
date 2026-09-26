"""测试共享工具。"""
from __future__ import annotations

import hashlib


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
