"""本地文件系统存储后端（骨架：接口定义 + 空实现）。"""

from __future__ import annotations

from pathlib import Path


class LocalStorage:
    """所有数据落在 root 目录下：

    root/
      namespaces/<ns>/objects/<key>/<version>.bin   # 已提交版本内容
      namespaces/<ns>/uploads/<session_id>/<part>   # 未完成的分段
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def part_path(self, namespace: str, session_id: str, number: int) -> Path:
        raise NotImplementedError

    def version_path(self, namespace: str, key: str, version: int) -> Path:
        raise NotImplementedError

    def write_part(self, namespace: str, session_id: str, number: int, data: bytes) -> Path:
        raise NotImplementedError

    def read_version(self, namespace: str, key: str, version: int) -> bytes:
        raise NotImplementedError

    def commit_parts(self, namespace: str, key: str, version: int, part_paths: list[Path]) -> Path:
        """原子地把分段合并为版本文件（先写临时文件再 rename）。"""
        raise NotImplementedError

    def delete_upload(self, namespace: str, session_id: str) -> None:
        raise NotImplementedError

    def delete_version(self, namespace: str, key: str, version: int) -> None:
        raise NotImplementedError
