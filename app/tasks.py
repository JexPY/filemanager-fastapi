import asyncio
import contextlib
import logging
import os
import uuid

import aiofiles
import aiofiles.os

from app.broker import broker
from app.config import settings
from app.services.metadata import MetadataError, UploadRecord, get_metadata_store
from app.services.storage import StorageError, delete_file, download_file, get_storage, upload_file
from app.services.webhooks import deliver_webhook

logger = logging.getLogger(__name__)


async def _fire_webhook(record: UploadRecord, event: str) -> None:
    """Best-effort completion webhook for a video record. No callback_url -> a
    no-op. deliver_webhook never raises, but guard anyway so nothing here can
    ever affect the task's own outcome."""
    if not record.callback_url:
        return
    try:
        await deliver_webhook(
            url=record.callback_url,
            event=event,
            data=record.to_public(),
            delivery_id=record.id,
        )
    except Exception:  # noqa: BLE001 -- delivery must never affect the task result
        logger.exception("Unexpected error delivering webhook for upload %s", record.id)


async def _probe_duration_seconds(input_path: str) -> float | None:
    """Return the input's duration in seconds via ffprobe, or None if it can't
    be determined. Best-effort: a probe failure must never fail the compression
    itself -- it only means we can't report `truncated` for this input."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            input_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=settings.FFMPEG_TIMEOUT_SECONDS
        )
        if process.returncode != 0:
            return None
        return float(stdout.decode().strip())
    except (ValueError, TimeoutError, OSError) as exc:
        logger.warning("ffprobe could not determine duration for %s: %s", input_path, exc)
        return None


@broker.task
async def compress_video_task(raw_storage_key: str, original_filename: str, upload_id: str) -> dict:
    unique_id = uuid.uuid4().hex
    input_path = f"/tmp/{unique_id}_{os.path.basename(raw_storage_key)}"
    output_path = f"/tmp/{unique_id}_compressed.mp4"
    output_key = f"videos/{unique_id}_compressed.mp4"
    max_duration = settings.VIDEO_MAX_DURATION_SECONDS

    store = await get_metadata_store()

    try:
        # Pull the raw upload from storage (only its key travels through Redis).
        raw_data = await download_file(raw_storage_key)
        async with aiofiles.open(input_path, "wb") as f:
            await f.write(raw_data)

        # Probe the input duration up front so we can tell the caller when the
        # output was truncated (input longer than the cap) instead of silently
        # dropping footage.
        input_duration = await _probe_duration_seconds(input_path)
        truncated = input_duration is not None and input_duration > max_duration

        # Run FFmpeg 7 async: H.264/AAC, cap output at max_duration, -crf 28,
        # -preset fast.
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-t",
            str(max_duration),
            "-c:v",
            "libx264",
            "-crf",
            "28",
            "-preset",
            "fast",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=settings.FFMPEG_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # Without this, a wedged ffmpeg (e.g. on a crafted/corrupt input)
            # would hang this worker slot forever -- process.communicate()
            # alone has no timeout.
            process.kill()
            await process.wait()
            raise RuntimeError(
                f"FFmpeg timed out after {settings.FFMPEG_TIMEOUT_SECONDS}s"
            ) from None

        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {stderr.decode(errors='replace')}")

        async with aiofiles.open(output_path, "rb") as f:
            output_data = await f.read()

        obj = await upload_file(output_data, output_key, "video/mp4")

        # Flip the record from `processing` to `ready`, pointing it at the
        # compressed object. A None result means the owner DELETEd the upload
        # while it was compressing -- don't leave the object we just wrote
        # orphaned with no record.
        record = await store.mark_ready(
            upload_id,
            storage_key=obj.key,
            size_bytes=obj.size,
            duration_seconds=input_duration,
            truncated=truncated,
        )
        if record is None:
            logger.warning(
                "Upload %s no longer exists (deleted mid-compression); discarding %s",
                upload_id,
                obj.key,
            )
            with contextlib.suppress(StorageError):
                await delete_file(obj.key)
            return {"status": "discarded", "upload_id": upload_id}

        # Prefer a presigned URL over the plain object URL when the backend
        # supports it (S3/R2/MinIO): the plain URL is unusable for a private
        # bucket.
        backend = await get_storage()
        presigned_url = await backend.presigned_get_url(obj.key)

        # Push completion to the client's callback (if any), so they don't have
        # to poll GET /tasks/{id}. Best-effort; never affects the task result.
        await _fire_webhook(record, "video.completed")

        return {
            "status": "success",
            "url": presigned_url or obj.url,
            "key": obj.key,
            "size": obj.size,
            "original_filename": original_filename,
            "upload_id": upload_id,
            "duration_seconds": input_duration,
            "truncated": truncated,
            "max_duration_seconds": max_duration,
        }

    except Exception:
        # Any failure (download, ffmpeg, upload, mark_ready) marks the record
        # `failed` so GET /files reflects it -- best-effort, then re-raise so the
        # TaskIQ result is an error too (GET /tasks/{id} -> failed). Fire a
        # `video.failed` webhook too, so a callback client learns about failures.
        failed_record: UploadRecord | None = None
        with contextlib.suppress(MetadataError):
            failed_record = await store.mark_failed(upload_id)
        if failed_record is not None:
            await _fire_webhook(failed_record, "video.failed")
        raise

    finally:
        # Cleanup temp files.
        for path in (input_path, output_path):
            if await aiofiles.os.path.exists(path):
                await aiofiles.os.remove(path)

        # The raw upload has already been fully consumed by ffmpeg above,
        # whether compression succeeded or failed -- it's never referenced
        # again, so leaving it in place would just accumulate storage forever
        # with no way to ever clean it up (delete_file was never called from
        # anywhere in the codebase before this). Best-effort: a cleanup
        # failure here must never mask the real task outcome.
        try:
            await delete_file(raw_storage_key)
        except StorageError as exc:
            logger.warning(
                "Failed to delete raw video %s after processing: %s", raw_storage_key, exc
            )
