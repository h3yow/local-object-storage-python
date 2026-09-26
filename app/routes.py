"""FastAPI 路由：把 HTTP 请求映射到 ObjectService。

认证方式：请求头 X-Access-Token。所有业务错误由 main.py 中的
异常处理器统一渲染为 {"error": {"reason", "message", "detail"}}。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Header, Query
from pydantic import BaseModel, Field

from .models import ObjectVersion, UploadSession
from .service import ObjectService


class CreateNamespaceRequest(BaseModel):
    quota_bytes: int = Field(ge=0)


class GrantRequest(BaseModel):
    token: str
    read: bool = False
    write: bool = False


class CreateUploadRequest(BaseModel):
    key: str
    expected_parts: int
    final_sha256: str
    metadata: dict = Field(default_factory=dict)


class UploadPartRequest(BaseModel):
    data_base64: str
    sha256: str


def _session_view(s: UploadSession) -> dict:
    return {
        "session_id": s.session_id,
        "namespace": s.namespace,
        "key": s.key,
        "state": s.state.value,
        "expected_parts": s.expected_parts,
        "final_sha256": s.final_sha256,
        "base_version": s.base_version,
        "uploaded_parts": sorted(s.parts),
        "expires_at": s.expires_at,
    }


def _version_view(v: ObjectVersion) -> dict:
    return {
        "version": v.version,
        "namespace": v.namespace,
        "key": v.key,
        "size": v.size,
        "sha256": v.sha256,
        "metadata": v.metadata,
        "created_at": v.created_at,
        "session_id": v.session_id,
        "deleted": v.deleted,
    }


def create_router(service: ObjectService) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # --- 命名空间与授权 ---
    @router.put("/namespaces/{namespace}", status_code=201)
    def create_namespace(namespace: str, req: CreateNamespaceRequest) -> dict:
        ns = service.create_namespace(namespace, req.quota_bytes)
        return {"namespace": ns.name, "quota_bytes": ns.quota_bytes, "used_bytes": ns.used_bytes}

    @router.get("/namespaces/{namespace}")
    def get_namespace(namespace: str) -> dict:
        ns = service._require_namespace(namespace)
        return {"namespace": ns.name, "quota_bytes": ns.quota_bytes, "used_bytes": ns.used_bytes}

    @router.post("/namespaces/{namespace}/grants", status_code=201)
    def grant(namespace: str, req: GrantRequest) -> dict:
        service.grant(namespace, req.token, read=req.read, write=req.write)
        return {"namespace": namespace, "token": req.token, "read": req.read, "write": req.write}

    # --- 分段上传 ---
    @router.post("/namespaces/{namespace}/uploads", status_code=201)
    def create_upload(namespace: str, req: CreateUploadRequest,
                      x_access_token: str | None = Header(default=None)) -> dict:
        service.authorize(namespace, x_access_token, "write")
        session = service.create_upload(
            namespace, req.key,
            expected_parts=req.expected_parts,
            final_sha256=req.final_sha256,
            metadata=req.metadata,
        )
        return _session_view(session)

    @router.put("/uploads/{session_id}/parts/{number}")
    def upload_part(session_id: str, number: int, req: UploadPartRequest,
                    x_access_token: str | None = Header(default=None)) -> dict:
        import base64

        session = service._sessions.get(session_id)
        if session is not None:
            service.authorize(session.namespace, x_access_token, "write")
        data = base64.b64decode(req.data_base64, validate=True)
        service.upload_part(session_id, number, data, req.sha256)
        return {"session_id": session_id, "part": number, "size": len(data)}

    @router.post("/uploads/{session_id}/complete", status_code=201)
    def complete_upload(session_id: str,
                        x_access_token: str | None = Header(default=None)) -> dict:
        session = service._sessions.get(session_id)
        if session is not None:
            service.authorize(session.namespace, x_access_token, "write")
        version = service.complete_upload(session_id)
        return _version_view(version)

    @router.post("/uploads/{session_id}/abort")
    def abort_upload(session_id: str,
                     x_access_token: str | None = Header(default=None)) -> dict:
        session = service._sessions.get(session_id)
        if session is not None:
            service.authorize(session.namespace, x_access_token, "write")
        service.abort_upload(session_id)
        return {"session_id": session_id, "state": "aborted"}

    # --- 读取 / 版本 / 删除 ---
    # 注意：versions 子路径必须先于 {key:path} 注册，否则会被路径转换器吞掉。
    @router.get("/namespaces/{namespace}/objects/{key:path}/versions")
    def list_versions(namespace: str, key: str,
                      x_access_token: str | None = Header(default=None)) -> dict:
        service.authorize(namespace, x_access_token, "read")
        return {"versions": [_version_view(v) for v in service.list_versions(namespace, key)]}

    @router.get("/namespaces/{namespace}/objects/{key:path}")
    def get_object(namespace: str, key: str,
                   version: int | None = Query(default=None),
                   x_access_token: str | None = Header(default=None)) -> dict:
        import base64

        service.authorize(namespace, x_access_token, "read")
        v, data = service.get_object(namespace, key, version)
        view = _version_view(v)
        view["data_base64"] = base64.b64encode(data).decode()
        return view

    @router.delete("/namespaces/{namespace}/objects/{key:path}")
    def delete_object(namespace: str, key: str,
                      x_access_token: str | None = Header(default=None)) -> dict:
        service.authorize(namespace, x_access_token, "write")
        return _version_view(service.delete_object(namespace, key))

    # --- 回收 ---
    @router.post("/maintenance/reap")
    def reap() -> dict:
        return {"reaped_sessions": service.reap_expired_sessions()}

    return router
