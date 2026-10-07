"""基于本地文件系统的存储层。

目录布局（根目录由 Service 决定）::

    root/
      namespaces.json              # 命名空间注册表（配额/令牌/已用字节）
      audit.log                    # 追加式 JSONL 审计日志
      ns/<namespace>/
        objects/<quoted-name>.json # 单个对象的全部版本元数据（原子替换）
        blobs/<sha256>             # 内容寻址的对象数据（不可变）
        uploads/<session_id>/
          session.json             # 会话状态（原子替换）
          parts/<seq>              # 已接收分段（不可变）

本层只做机械的、崩溃安全的持久化；并发判定、配额、授权等业务规则在 service 层。
单进程内以 threading.RLock 串行化所有变更（部署需单 worker）。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, unquote


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def encode_name(name: str) -> str:
    """把对象名编码为安全的单一文件名（允许原始名称含 "/"）。"""
    return quote(name, safe="")


def decode_name(fname: str) -> str:
    return unquote(fname)


class LocalStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._registry_path = self.root / "namespaces.json"
        self._audit_path = self.root / "audit.log"

    # ---- 锁与基础 IO ----
    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            yield

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        # os.replace 原子替换保证目标文件存在时始终完整。
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # ---- 审计 ----
    def append_audit(self, event: dict[str, Any]) -> None:
        event = {"ts": iso(utcnow()), **event}
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ---- 命名空间 ----
    def _read_registry(self) -> dict[str, dict[str, Any]]:
        return self._read_json(self._registry_path, {})

    def _write_registry(self, reg: dict[str, dict[str, Any]]) -> None:
        self._write_json(self._registry_path, reg)

    def namespace_exists(self, namespace: str) -> bool:
        return namespace in self._read_registry()

    def get_namespace(self, namespace: str) -> dict[str, Any] | None:
        return self._read_registry().get(namespace)

    def create_namespace(
        self, namespace: str, quota_bytes: int, tokens: list[str],
        max_history_versions: int | None = None,
    ) -> dict[str, Any]:
        reg = self._read_registry()
        if namespace in reg:
            raise FileExistsError(namespace)
        record = {
            "name": namespace,
            "quota_bytes": int(quota_bytes),
            "tokens": list(tokens),
            "used_bytes": 0,
            "inflight_bytes": 0,
            "max_history_versions": max_history_versions,
        }
        reg[namespace] = record
        self._write_registry(reg)
        self._ns_dir(namespace).mkdir(parents=True, exist_ok=True)
        return record

    def list_namespaces(self) -> list[str]:
        return sorted(self._read_registry())

    def update_namespace(self, namespace: str, **changes: Any) -> dict[str, Any]:
        reg = self._read_registry()
        record = reg[namespace]
        record.update(changes)
        reg[namespace] = record
        self._write_registry(reg)
        return record

    def reconcile_namespace_usage(self, namespace: str) -> dict[str, Any]:
        """从磁盘真相重算命名空间用量并持久化（服务启动时调用，兼容旧数据）。

        - used_bytes    = 所有对象全部保留版本的逻辑大小之和
        - inflight_bytes = 仍处于 open 且未过期会话的已接收分段字节之和；
          同时把每个会话的 ``inflight_bytes`` 字段规范化，保证后续
          complete/abort/expire 的释放是幂等且不重复的。
        """
        used = 0
        for meta in self.list_object_metas(namespace):
            for v in meta.get("versions", []):
                used += int(v.get("size", 0))
        inflight = 0
        now = utcnow()
        for s in self.all_sessions(namespace):
            counted = 0
            if s.get("status") == "open" and parse_iso(s["expires_at"]) > now:
                counted = sum(int(p.get("size", 0))
                              for p in s.get("parts", {}).values())
            if s.get("inflight_bytes") != counted:
                s["inflight_bytes"] = counted
                self.save_session(namespace, s)
            inflight += counted
        return self.update_namespace(
            namespace, used_bytes=used, inflight_bytes=inflight)

    def _ns_dir(self, namespace: str) -> Path:
        return self.root / "ns" / namespace

    # ---- 对象元数据 ----
    def _object_meta_path(self, namespace: str, object_name: str) -> Path:
        return self._ns_dir(namespace) / "objects" / f"{encode_name(object_name)}.json"

    def get_object_meta(self, namespace: str, object_name: str) -> dict[str, Any] | None:
        path = self._object_meta_path(namespace, object_name)
        return self._read_json(path, None)

    def put_object_meta(self, namespace: str, meta: dict[str, Any]) -> None:
        self._write_json(self._object_meta_path(namespace, meta["object_name"]), meta)

    def list_object_metas(self, namespace: str) -> list[dict[str, Any]]:
        d = self._ns_dir(namespace) / "objects"
        if not d.exists():
            return []
        metas = []
        for p in sorted(d.glob("*.json")):
            data = self._read_json(p, None)
            if data is not None:
                metas.append(data)
        return metas

    # ---- blob（内容寻址，不可变）----
    def _blobs_dir(self, namespace: str) -> Path:
        return self._ns_dir(namespace) / "blobs"

    def blob_path(self, namespace: str, checksum: str) -> Path:
        return self._blobs_dir(namespace) / checksum

    def blob_exists(self, namespace: str, checksum: str) -> bool:
        return self.blob_path(namespace, checksum).exists()

    def blob_referenced(self, namespace: str, checksum: str,
                        exclude_object: str | None = None) -> bool:
        """blob 是否仍被某个（现存）版本引用。"""
        for meta in self.list_object_metas(namespace):
            if exclude_object is not None and meta["object_name"] == exclude_object:
                continue
            for v in meta.get("versions", []):
                if v["checksum"] == checksum:
                    return True
        return False

    def remove_blob_if_unreferferenced(self, namespace: str, checksum: str,
                                       exclude_object: str | None = None) -> bool:
        if self.blob_referenced(namespace, checksum, exclude_object):
            return False
        p = self.blob_path(namespace, checksum)
        try:
            p.unlink()
            return True
        except FileNotFoundError:
            return False

    def new_temp_blob(self, namespace: str) -> Path:
        d = self._blobs_dir(namespace)
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile_in_dir(d)
        os.close(fd)
        return Path(tmp)

    # ---- 上传会话 ----
    def _uploads_dir(self, namespace: str) -> Path:
        return self._ns_dir(namespace) / "uploads"

    def _session_dir(self, namespace: str, session_id: str) -> Path:
        return self._uploads_dir(namespace) / session_id

    def session_path(self, namespace: str, session_id: str) -> Path:
        return self._session_dir(namespace, session_id) / "session.json"

    def part_path(self, namespace: str, session_id: str, seq: int) -> Path:
        return self._session_dir(namespace, session_id) / "parts" / f"{seq:08d}"

    def save_session(self, namespace: str, session: dict[str, Any]) -> None:
        sid = session["session_id"]
        self._session_dir(namespace, sid).mkdir(parents=True, exist_ok=True)
        self._write_json(self.session_path(namespace, sid), session)

    def load_session(self, namespace: str, session_id: str) -> dict[str, Any] | None:
        return self._read_json(self.session_path(namespace, session_id), None)

    def all_sessions(self, namespace: str) -> list[dict[str, Any]]:
        d = self._uploads_dir(namespace)
        if not d.exists():
            return []
        out = []
        for sub in sorted(d.iterdir()):
            data = self._read_json(sub / "session.json", None)
            if data is not None:
                out.append(data)
        return out

    def remove_session_parts(self, namespace: str, session_id: str) -> None:
        """只删除分段数据目录，保留 session.json 作为完成/失效留痕。"""
        parts = self._session_dir(namespace, session_id) / "parts"
        shutil.rmtree(parts, ignore_errors=True)

    def remove_session_dir(self, namespace: str, session_id: str) -> None:
        shutil.rmtree(self._session_dir(namespace, session_id), ignore_errors=True)

    # ---- 时间工具 ----
    @staticmethod
    def ttl_expiry(ttl_seconds: int, *, now: datetime | None = None) -> tuple[str, str]:
        now = now or utcnow()
        return iso(now), iso(now + timedelta(seconds=ttl_seconds))


def tempfile_in_dir(d: Path) -> tuple[int, str]:
    import tempfile

    return tempfile.mkstemp(dir=str(d), prefix=".assemble-")
