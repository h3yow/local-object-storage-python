"""本地文件系统存储后端。

目录布局：
  root/
    namespaces/<ns>/objects/<key>/<version>.bin   # 已提交版本内容（原子 rename 落盘）
    namespaces/<ns>/uploads/<session_id>/part-<n> # 未完成的分段

所有写操作都先写临时文件再 os.replace，保证不会暴露部分写入。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def _safe_component(value: str) -> str:
    """防止路径穿越：命名空间/会话号/键中的路径分隔符一律转码。"""
    return (
        value.replace("%", "%25")
        .replace("/", "%2F")
        .replace("\\", "%5C")
        .replace("..", "%2E%2E")
    )


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- 路径计算 ---
    def _ns_dir(self, namespace: str) -> Path:
        return self.root / "namespaces" / _safe_component(namespace)

    def _upload_dir(self, namespace: str, session_id: str) -> Path:
        return self._ns_dir(namespace) / "uploads" / _safe_component(session_id)

    def _object_dir(self, namespace: str, key: str) -> Path:
        return self._ns_dir(namespace) / "objects" / _safe_component(key)

    def part_path(self, namespace: str, session_id: str, number: int) -> Path:
        return self._upload_dir(namespace, session_id) / f"part-{number:06d}"

    def version_path(self, namespace: str, key: str, version: int) -> Path:
        return self._object_dir(namespace, key) / f"v{version:08d}.bin"

    # --- 写入 ---
    def _atomic_write(self, target: Path, data: bytes) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return target

    def write_part(self, namespace: str, session_id: str, number: int, data: bytes) -> Path:
        """分段落盘：原子替换，重复提交同一分段（内容一致）是幂等的。"""
        return self._atomic_write(self.part_path(namespace, session_id, number), data)

    def read_version(self, namespace: str, key: str, version: int) -> bytes:
        return self.version_path(namespace, key, version).read_bytes()

    def commit_parts(self, namespace: str, key: str, version: int, part_paths: list[Path]) -> Path:
        """把有序分段合并为版本文件：先拼到临时文件，再一次 rename 对外可见。"""
        target = self.version_path(namespace, key, version)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as out:
                for p in part_paths:
                    with open(p, "rb") as f:
                        shutil.copyfileobj(f, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return target

    # --- 清理 ---
    def delete_upload(self, namespace: str, session_id: str) -> None:
        shutil.rmtree(self._upload_dir(namespace, session_id), ignore_errors=True)

    def delete_version(self, namespace: str, key: str, version: int) -> None:
        try:
            os.unlink(self.version_path(namespace, key, version))
        except FileNotFoundError:
            pass
