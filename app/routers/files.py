import hmac
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.broker import broker
from app.config import settings
from app.services.image_vips import validate_and_strip_image
from app.services.imgproxy import generate_signed_url
from app.services.qr_generator import generate_qr_image
from app.services.storage import StorageError, upload_file
from app.tasks import compress_video_task

router = APIRouter()
security = HTTPBearer(auto_error=False)


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    # auto_error=False so a missing/malformed Authorization header lands here
    # too, instead of Starlette's default 403 -- missing and wrong credentials
    # should both be 401.
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = credentials.credentials
    # Constant-time comparison against each configured token to avoid timing leaks.
    is_valid = any(hmac.compare_digest(token, t) for t in settings.valid_tokens)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return token


@router.post("/generate/qrcode", dependencies=[Depends(verify_token)])
async def generate_qrcode(content: str = Form(...)):
    try:
        png_data = generate_qr_image(content)
        return Response(content=png_data, media_type="image/png")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/upload/image", dependencies=[Depends(verify_token)])
async def upload_image(file: UploadFile = File(...)):
    file_data = await file.read()

    # Client-side failures (bad/unsupported image) => 400 with the validation message.
    try:
        optimized_buffer, content_type, width, height = validate_and_strip_image(file_data)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    unique_id = uuid.uuid4().hex
    object_name = f"images/{unique_id}.webp"

    # Storage failures are server-side; never surface backend internals to the caller.
    try:
        obj = await upload_file(optimized_buffer, object_name, content_type)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Storage backend unavailable"
        ) from exc

    thumbnail_url = generate_signed_url(obj.url, processing_options="rs:fill:300:300")
    original_optimized_url = generate_signed_url(obj.url, processing_options="rs:auto")

    return {
        "status": "success",
        "dimensions": {"width": width, "height": height},
        "raw_url": obj.url,
        "imgproxy_thumbnail_url": thumbnail_url,
        "imgproxy_optimized_url": original_optimized_url,
    }


@router.post(
    "/upload/video", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(verify_token)]
)
async def upload_video(file: UploadFile = File(...)):
    file_data = await file.read()

    # Stage the raw upload in storage; only its key travels through Redis.
    original_filename = file.filename or "video.mp4"
    ext = original_filename.rsplit(".", 1)[-1] if "." in original_filename else "mp4"
    raw_key = f"raw/videos/{uuid.uuid4().hex}.{ext}"
    try:
        await upload_file(
            file_data,
            raw_key,
            file.content_type or "application/octet-stream",
        )
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Storage backend unavailable"
        ) from exc

    # Enqueue the task with the lightweight key reference.
    task = await compress_video_task.kiq(
        raw_storage_key=raw_key,
        original_filename=original_filename,
    )

    return {
        "status": "accepted",
        "task_id": task.task_id,
        "raw_key": raw_key,
    }


@router.get("/tasks/{task_id}", dependencies=[Depends(verify_token)])
async def get_task_status(task_id: str):
    is_ready = await broker.result_backend.is_result_ready(task_id)
    if not is_ready:
        return {"task_id": task_id, "status": "pending"}

    task_result = await broker.result_backend.get_result(task_id)
    if task_result.is_err:
        return {"task_id": task_id, "status": "failed", "error": str(task_result.error)}

    return {"task_id": task_id, "status": "completed", "result": task_result.return_value}
