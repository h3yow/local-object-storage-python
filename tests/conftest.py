import base64
import hashlib
import logging
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.errors import StorageError
from app.routes import create_router
from app.service import ObjectService
from app.storage import LocalStorage


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture()
def service(tmp_path):
    return ObjectService(LocalStorage(tmp_path / "data"), session_ttl=3600.0)


@pytest.fixture()
def client(tmp_path):
    app = FastAPI()
    svc = ObjectService(LocalStorage(tmp_path / "data"), session_ttl=3600.0)
    app.include_router(create_router(svc))

    @app.exception_handler(StorageError)
    async def _handler(request, exc: StorageError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"reason": exc.reason, "message": exc.message, "detail": exc.detail}},
        )

    app.state.service = svc
    return TestClient(app)


@pytest.fixture(autouse=True)
def configure_logging(caplog):
    caplog.set_level(logging.INFO, logger="objectstore")
