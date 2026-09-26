import tempfile

import uvicorn
from fastapi import FastAPI

from app.errors import StorageError
from app.routes import create_router
from app.service import ObjectService
from app.storage import LocalStorage

import os

DATA_ROOT = os.environ.get("OBJECT_STORE_ROOT", tempfile.mkdtemp(prefix="objstore-"))

storage = LocalStorage(DATA_ROOT)
service = ObjectService(storage)
app = FastAPI(title="local-object-storage")
app.include_router(create_router(service))


@app.exception_handler(StorageError)
async def storage_error_handler(request, exc: StorageError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"reason": exc.reason, "message": exc.message, "detail": exc.detail}},
    )


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
