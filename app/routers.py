"""HTTP 路由层。

约定：
- 路径形如 /namespaces/{ns}/...；令牌通过 ``X-Namespace-Token`` 头传递。
- 分段数据放在请求体 raw bytes，摘要在 ``X-Part-Checksum`` 头（sha256 hex）。
- 所有 ServiceError 统一转成 ``{"error": {code,message,details}}``。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response

from . import errors
from .models import (
    CompleteResult,
    ListResult,
    NamespaceCreate,
    NamespaceInfo,
    ObjectInfo,
    RestoreRequest,
    RestoreResult,
    SessionCreate,
    SessionInfo,
)
from .service import ObjectService

router = APIRouter()
log = logging.getLogger("object_storage.api")

# 存储根目录与服务单例在 app 工厂里通过 router.state 注入；这里提供默认本地路径。
DEFAULT_ROOT = Path.cwd() / ".objectstore"


def get_service(request: Request) -> ObjectService:
    svc: ObjectService | None = getattr(request.app.state, "service", None)
    if svc is None:
        svc = ObjectService(DEFAULT_ROOT)
        request.app.state.service = svc
    return svc


def _token(x_namespace_token: str | None) -> str | None:
    return x_namespace_token


# ---------- 命名空间 ----------
@router.post("/namespaces", response_model=NamespaceInfo, status_code=201)
def create_namespace(body: NamespaceCreate,
                     svc: ObjectService = Depends(get_service)) -> NamespaceInfo:
    return svc.create_namespace(body)


@router.get("/namespaces/{namespace}", response_model=NamespaceInfo)
def get_namespace(namespace: str,
                  svc: ObjectService = Depends(get_service)) -> NamespaceInfo:
    return svc.get_namespace_info(namespace)


# ---------- 上传会话 ----------
@router.post("/namespaces/{namespace}/uploads", response_model=SessionInfo,
             status_code=201)
def create_session(namespace: str, body: SessionCreate,
                   x_namespace_token: str | None = Header(default=None),
                   svc: ObjectService = Depends(get_service)) -> SessionInfo:
    return svc.create_session(namespace, _token(x_namespace_token), body)


@router.get("/namespaces/{namespace}/uploads/{session_id}",
            response_model=SessionInfo)
def get_session(namespace: str, session_id: str,
                x_namespace_token: str | None = Header(default=None),
                svc: ObjectService = Depends(get_service)) -> SessionInfo:
    svc.authorize(namespace, _token(x_namespace_token))
    return svc.get_session(namespace, session_id)


@router.post("/namespaces/{namespace}/uploads/{session_id}/abort",
             response_model=SessionInfo)
def abort_session(namespace: str, session_id: str,
                  x_namespace_token: str | None = Header(default=None),
                  svc: ObjectService = Depends(get_service)) -> SessionInfo:
    svc.authorize(namespace, _token(x_namespace_token))
    return svc.abort_session(namespace, session_id)


@router.put("/namespaces/{namespace}/uploads/{session_id}/parts/{seq}")
async def upload_part(namespace: str, session_id: str, seq: int,
                      request: Request,
                      x_part_checksum: str = Header(...),
                      x_namespace_token: str | None = Header(default=None),
                      svc: ObjectService = Depends(get_service)) -> dict:
    svc.authorize(namespace, _token(x_namespace_token))
    data = await request.body()
    return svc.upload_part(namespace, session_id, seq, data, x_part_checksum.strip())


@router.post("/namespaces/{namespace}/uploads/{session_id}/complete",
             response_model=CompleteResult, status_code=201)
def complete_session(namespace: str, session_id: str,
                     x_namespace_token: str | None = Header(default=None),
                     svc: ObjectService = Depends(get_service)) -> CompleteResult:
    svc.authorize(namespace, _token(x_namespace_token))
    return svc.complete_session(namespace, session_id)


# ---------- 对象读取 / 列表 / 删除 ----------
@router.get("/namespaces/{namespace}/objects", response_model=ListResult)
def list_objects(namespace: str,
                 x_namespace_token: str | None = Header(default=None),
                 svc: ObjectService = Depends(get_service)) -> ListResult:
    svc.authorize(namespace, _token(x_namespace_token))
    return ListResult(namespace=namespace,
                      objects=svc.list_objects(namespace))


# 注意：/content 子路径必须在 {object_name:path} 通配之前注册，
# 否则 path 转换器会吞掉 "/content" 段。
# 同理，/versions/{version} 与 /restore 子路径也注册在通配路由之前。
@router.delete("/namespaces/{namespace}/objects/{object_name:path}/versions/{version}")
def delete_version(namespace: str, object_name: str, version: int,
                   x_namespace_token: str | None = Header(default=None),
                   svc: ObjectService = Depends(get_service)) -> dict:
    svc.authorize(namespace, _token(x_namespace_token))
    return svc.delete_version(namespace, object_name, version)


@router.post("/namespaces/{namespace}/objects/{object_name:path}/restore",
             response_model=RestoreResult, status_code=201)
def restore_object(namespace: str, object_name: str, body: RestoreRequest,
                   x_namespace_token: str | None = Header(default=None),
                   svc: ObjectService = Depends(get_service)) -> RestoreResult:
    svc.authorize(namespace, _token(x_namespace_token))
    return svc.restore_object(namespace, object_name, body)


@router.get("/namespaces/{namespace}/objects/{object_name:path}/content")
def download_object(namespace: str, object_name: str,
                    version: int | None = Query(default=None),
                    x_namespace_token: str | None = Header(default=None),
                    svc: ObjectService = Depends(get_service)) -> Response:
    svc.authorize(namespace, _token(x_namespace_token))
    vmeta, path, vno = svc.get_object(namespace, object_name, version)
    data = path.read_bytes()
    return Response(
        content=data, media_type=vmeta.get("content_type") or "application/octet-stream",
        headers={
            "ETag": f'"{vmeta["checksum"]}"',
            "X-Object-Version": str(vno),
            "X-Object-Checksum": vmeta["checksum"],
        },
    )


@router.get("/namespaces/{namespace}/objects/{object_name:path}",
            response_model=ObjectInfo)
def stat_object(namespace: str, object_name: str,
                version: int | None = Query(default=None),
                x_namespace_token: str | None = Header(default=None),
                svc: ObjectService = Depends(get_service)) -> ObjectInfo:
    svc.authorize(namespace, _token(x_namespace_token))
    if version is not None:
        # 校验版本存在，404 可区分 version_not_found
        svc.get_object(namespace, object_name, version)
    return svc.stat_object(namespace, object_name)


@router.delete("/namespaces/{namespace}/objects/{object_name:path}")
def delete_object(namespace: str, object_name: str,
                  x_namespace_token: str | None = Header(default=None),
                  svc: ObjectService = Depends(get_service)) -> dict:
    svc.authorize(namespace, _token(x_namespace_token))
    return svc.delete_object(namespace, object_name)


# ---------- 运维：回收过期会话 ----------
@router.post("/namespaces/{namespace}/maintenance/purge-expired")
def purge_expired(namespace: str,
                  x_namespace_token: str | None = Header(default=None),
                  svc: ObjectService = Depends(get_service)) -> dict:
    svc.authorize(namespace, _token(x_namespace_token))
    return svc.purge_expired_sessions(namespace)
