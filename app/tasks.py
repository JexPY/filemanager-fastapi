import asyncio
import contextlib
import logging
import os
import uuid

import aiofiles
import aiofiles.os

from app.broker import broker
from app.config import settings
from app.services.image_vips import validate_and_strip_image
from app.services.metadata import (
    KIND_IMAGE,
    KIND_VIDEO,
    STATUS_READY,
    WEBHOOK_DELIVERED,
    WEBHOOK_FAILED,
    WEBHOOK_PENDING,
    MetadataError,
    UploadRecord,
    get_metadata_store,
)
from app.services.storage import StorageError, delete_file, download_file, get_storage, upload_file
from app.services.webhooks import deliver_webhook

logger = logging.getLogger(__name__)


async def _enqueue_webhook(record: UploadRecord, event: str) -> None:
    """Enqueue delivery of a terminal-state webhook onto its own TaskIQ task, so
    a slow/dead receiver never blocks the compression worker slot. No
    callback_url -> a no-op. Best-effort: a failure to enqueue (e.g. Redis
    hiccup) must never affect the compression task's own outcome -- the owner can
    still replay via POST /files/{id}/redeliver."""
    if not record.callback_url:
        return
    try:
        await deliver_webhook_task.kiq(upload_id=record.id, event=event)
    except Exception:  # noqa: BLE001 -- enqueue must never affect the task result
        logger.exception("Failed to enqueue webhook delivery for upload %s", record.id)


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

        # Push completion to the client's callback (if any) on its own task, so
        # they don't have to poll GET /tasks/{id} and a slow receiver can't block
        # this worker slot. Best-effort; never affects the task result.
        await _enqueue_webhook(record, "video.completed")

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
            await _enqueue_webhook(failed_record, "video.failed")
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


@broker.task
async def deliver_webhook_task(upload_id: str, event: str) -> dict:
    """Deliver a terminal-state webhook on its own worker slot, decoupled from
    compression. Re-loads the record by id (so the payload always reflects
    current state, incl. a later poster/redelivery), signs + POSTs it, and
    persists the delivery outcome on the row as a durable dead-letter record
    (webhook_status/attempts/last_error). Never raises."""
    store = await get_metadata_store()
    try:
        record = await store.get_by_id(upload_id)
    except MetadataError:
        logger.exception("Webhook task could not load upload %s", upload_id)
        return {"status": "skipped", "reason": "load failed", "upload_id": upload_id}
    if record is None or not record.callback_url:
        return {"status": "skipped", "reason": "no callback", "upload_id": upload_id}

    with contextlib.suppress(MetadataError):
        await store.mark_webhook(upload_id, status=WEBHOOK_PENDING)

    result = await deliver_webhook(
        url=record.callback_url,
        event=event,
        data=record.to_public(),
        delivery_id=record.id,
    )
    webhook_status = WEBHOOK_DELIVERED if result.delivered else WEBHOOK_FAILED
    with contextlib.suppress(MetadataError):
        await store.mark_webhook(
            upload_id,
            status=webhook_status,
            attempts=result.attempts,
            last_error=result.last_error,
        )
    return {
        "status": webhook_status,
        "upload_id": upload_id,
        "event": event,
        "attempts": result.attempts,
    }


async def _poster_seek_seconds(input_path: str) -> float:
    """Where to grab the poster frame from: ~10% in (so it's not a black
    lead-in frame), clamped just inside the clip. Falls back to the very first
    frame (0s) when the duration can't be probed."""
    duration = await _probe_duration_seconds(input_path)
    if duration is None or duration <= 0:
        return 0.0
    return min(duration * 0.1, max(duration - 0.1, 0.0))


@broker.task
async def generate_poster_task(upload_id: str, at_seconds: float | None = None) -> dict:
    """Extract a poster frame from a *ready* video and store it as its own image
    `uploads` record, linked back from the video via poster_upload_id. Reuses
    the image pipeline end to end: ffmpeg single-frame extract -> pyvips
    validate/strip -> WebP. The api admits+enqueues this (owner-scoped); the
    worker has the video bytes and ffmpeg/pyvips."""
    store = await get_metadata_store()
    video = await store.get_by_id(upload_id)
    if video is None:
        return {"status": "skipped", "reason": "video gone", "upload_id": upload_id}
    if video.kind != KIND_VIDEO or video.status != STATUS_READY:
        # The route guards this; be defensive if state changed after enqueue.
        return {"status": "skipped", "reason": "not a ready video", "upload_id": upload_id}

    unique_id = uuid.uuid4().hex
    src_key = video.storage_key  # the compressed mp4
    input_path = f"/tmp/{unique_id}_{os.path.basename(src_key)}"
    frame_path = f"/tmp/{unique_id}_poster.png"

    try:
        raw_data = await download_file(src_key)
        async with aiofiles.open(input_path, "wb") as f:
            await f.write(raw_data)

        seek = at_seconds if at_seconds is not None else await _poster_seek_seconds(input_path)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-ss",
            f"{seek:.3f}",
            "-i",
            input_path,
            "-frames:v",
            "1",
            "-f",
            "image2",
            "-c:v",
            "png",
            frame_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=settings.FFMPEG_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(
                f"Poster ffmpeg timed out after {settings.FFMPEG_TIMEOUT_SECONDS}s"
            ) from None
        if process.returncode != 0:
            raise RuntimeError(f"Poster extract failed: {stderr.decode(errors='replace')}")

        async with aiofiles.open(frame_path, "rb") as f:
            frame_bytes = await f.read()

        # Reuse the exact image validate/strip path (CPU-bound -> threadpool).
        webp_bytes, content_type, width, height = await asyncio.to_thread(
            validate_and_strip_image, frame_bytes
        )
        poster_key = f"posters/{unique_id}.webp"
        obj = await upload_file(webp_bytes, poster_key, content_type)

        poster = await store.create(
            owner=video.owner,
            kind=KIND_IMAGE,
            storage_key=obj.key,
            content_type=content_type,
            size_bytes=obj.size,
            status=STATUS_READY,
            width=width,
            height=height,
        )

        # Link the video to its poster. None means the owner DELETEd the video
        # mid-generation -- discard the poster we just wrote (object + record)
        # instead of orphaning it, mirroring compress_video_task's mark_ready.
        linked = await store.set_poster(upload_id, poster.id)
        if linked is None:
            logger.warning(
                "Video %s gone during poster generation; discarding %s", upload_id, obj.key
            )
            with contextlib.suppress(StorageError):
                await delete_file(obj.key)
            with contextlib.suppress(MetadataError):
                await store.delete(poster.id, video.owner)
            return {"status": "discarded", "upload_id": upload_id}

        return {
            "status": "success",
            "upload_id": upload_id,
            "poster_id": poster.id,
            "key": obj.key,
            "width": width,
            "height": height,
        }

    finally:
        for path in (input_path, frame_path):
            if await aiofiles.os.path.exists(path):
                await aiofiles.os.remove(path)
