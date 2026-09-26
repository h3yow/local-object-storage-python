"""FastAPI 路由（骨架）。"""

from __future__ import annotations

from fastapi import APIRouter

from .service import ObjectService


def create_router(service: ObjectService) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return router
