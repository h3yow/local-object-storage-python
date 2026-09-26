"""公共测试夹具：临时存储目录 + TestClient + 上传助手。"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import ObjectService


from utils import sha256  # noqa: F401


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    root.mkdir()
    return root


@pytest.fixture
def service(tmp_store: Path) -> ObjectService:
    return ObjectService(tmp_store)


@pytest.fixture
def client(tmp_store: Path) -> TestClient:
    return TestClient(create_app(tmp_store))


@pytest.fixture
def caplog_pretty(caplog: pytest.LogCaptureFixture):
    """以固定格式输出 object_storage 日志（会话/版本/判定依据）。"""
    handler = caplog.handler
    handler.setFormatter(
        logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    caplog.set_level(logging.INFO, logger="object_storage")
    return caplog


@pytest.fixture
def make_namespace(client: TestClient):
    def _make(name: str = "team-a", quota_bytes: int = 0,
              tokens: list[str] | None = None) -> dict[str, Any]:
        tokens = tokens if tokens is not None else ["token-a", "token-admin"]
        r = client.post("/namespaces",
                        json={"name": name, "quota_bytes": quota_bytes,
                              "tokens": tokens})
        assert r.status_code == 201, r.text
        return r.json()
    return _make


@pytest.fixture
def uploader(client: TestClient, make_namespace):
    """封装一次完整的 HTTP 分段上传，返回每步响应。"""
    make_namespace()

    def _upload(object_name: str, parts: list[bytes], token: str = "token-a",
                *, final_checksum: str | None = None,
                expected_version: int | None = None,
                namespace: str = "team-a",
                checksums: list[str] | None = None) -> dict[str, Any]:
        final = final_checksum or sha256(b"".join(parts))
        body: dict[str, Any] = {
            "object_name": object_name,
            "total_parts": len(parts),
            "final_checksum": final,
            "metadata": {"source": "pytest"},
        }
        if expected_version is not None:
            body["expected_version"] = expected_version
        r = client.post(f"/namespaces/{namespace}/uploads",
                        headers={"X-Namespace-Token": token}, json=body)
        assert r.status_code == 201, r.text
        sid = r.json()["session_id"]
        part_responses = []
        sums = checksums or [sha256(p) for p in parts]
        for i, (p, c) in enumerate(zip(parts, sums), start=1):
            pr = client.put(
                f"/namespaces/{namespace}/uploads/{sid}/parts/{i}",
                headers={"X-Namespace-Token": token, "X-Part-Checksum": c},
                content=p)
            part_responses.append(pr)
        done = client.post(
            f"/namespaces/{namespace}/uploads/{sid}/complete",
            headers={"X-Namespace-Token": token})
        return {"session": r, "sid": sid, "parts": part_responses,
                "complete": done}
    return _upload
