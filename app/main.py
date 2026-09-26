"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import ServiceError
from .routers import router
from .service import ObjectService


def create_app(storage_root: str | Path | None = None) -> FastAPI:
    logging.basicConfig(
        level=os.environ.get("OBJECTSTORAGE_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = FastAPI(
        title="local-object-storage",
        version="0.1.0",
        description="仅依赖本地存储的对象服务：分段续传、版本、配额与命名空间授权。",
    )
    root = Path(storage_root or os.environ.get("OBJECTSTORAGE_ROOT", ".objectstore"))
    app.state.service = ObjectService(root)
    app.include_router(router)

    @app.exception_handler(ServiceError)
    async def _on_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        logging.getLogger("object_storage.api").warning(
            "service_error code=%s message=%s details=%s",
            exc.error_code, exc.message, exc.details)
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "storage_root": str(root)}

    return app


app = create_app()
