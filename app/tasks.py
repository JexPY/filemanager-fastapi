import asyncio
import contextlib
import json
import logging
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
from app.services.storage import StorageError, delete_file, get_storage, upload_file
from app.services.webhooks import deliver_webhook

logger = logging.getLogger(__name__)


async def _resolve_ffmpeg_input(storage_key: str) -> str:
    """Resolve an ffmpeg ``-i`` source for a stored object WITHOUT buffering its
    bytes through worker RAM -- the input side is the real memory waste in the
    video pipeline (an N-hundred-MB upload was previously downloaded whole, then
    rewritten to /tmp, before ffmpeg even started). Mirrors resolve_playback's
    per-backend split via the storage singleton (itself selected by
    STORAGE_BACKEND):

    * local  -> the object's on-disk path in the shared media volume; ffmpeg
      reads it directly, no copy at all.
    * s3/gcp -> a short-lived presigned GET URL; ffmpeg streams it over HTTPS
      (range requests to seek), so nothing is downloaded first.

    The storage key is UUID-based and already sanitized upstream; the local path
    still passes through LocalStorage's traversal guard.
    """
    backend = await get_storage()
    local = backend.local_path(storage_key)
    if local is not None:
        return local
    signed = await backend.presigned_get_url(storage_key, settings.FFMPEG_INPUT_URL_TTL_SECONDS)
    if not signed:
        raise StorageError(f"Backend cannot produce an ffmpeg input source for {storage_key!r}")
    return signed


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


async def _probe_video_metadata(input_path: str) -> tuple[float | None, int | None, int | None]:
    """Return the input's (duration, width, height) via ffprobe.
    Best-effort: a probe failure must never fail the compression itself."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            input_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=settings.FFMPEG_TIMEOUT_SECONDS
        )
        if process.returncode != 0:
            return None, None, None

        data = json.loads(stdout.decode())
        duration = (
            float(data.get("format", {}).get("duration"))
            if data.get("format", {}).get("duration")
            else None
        )

        width, height = None, None
        streams = data.get("streams", [])
        if streams:
            width = streams[0].get("width")
            height = streams[0].get("height")

        return duration, width, height
    except (ValueError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.warning("ffprobe could not determine metadata for %s: %s", input_path, exc)
        return None, None, None


@broker.task
async def compress_video_task(
    raw_storage_key: str,
    original_filename: str,
    upload_id: str,
    output_format: str = "mp4",
    optimization: str = "balanced",
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> dict:
    unique_id = uuid.uuid4().hex

    ext = "webm" if output_format.startswith("webm") else "mp4"
    content_type = f"video/{ext}"
    output_path = f"/tmp/{unique_id}_compressed.{ext}"
    output_key = f"videos/{unique_id}_compressed.{ext}"

    store = await get_metadata_store()

    try:
        input_source = await _resolve_ffmpeg_input(raw_storage_key)
        input_duration, width, height = await _probe_video_metadata(input_source)

        truncated = start_seconds is not None or end_seconds is not None

        ffmpeg_args = ["ffmpeg", "-y"]
        if start_seconds is not None:
            ffmpeg_args.extend(["-ss", str(start_seconds)])
        if end_seconds is not None:
            ffmpeg_args.extend(["-to", str(end_seconds)])

        ffmpeg_args.extend(["-i", input_source])

        if optimization == "balanced":
            ffmpeg_args.extend(["-vf", "scale='min(1280,iw)':-2"])
            if output_format == "webm_vp9":
                ffmpeg_args.extend(
                    [
                        "-c:v",
                        "libvpx-vp9",
                        "-crf",
                        "35",
                        "-b:v",
                        "0",
                        "-cpu-used",
                        "4",
                        "-c:a",
                        "libopus",
                        "-b:a",
                        "96k",
                    ]
                )
            elif output_format == "webm_av1":
                ffmpeg_args.extend(
                    [
                        "-c:v",
                        "libsvtav1",
                        "-preset",
                        "8",
                        "-crf",
                        "45",
                        "-c:a",
                        "libopus",
                        "-b:a",
                        "96k",
                    ]
                )
            else:
                ffmpeg_args.extend(
                    [
                        "-c:v",
                        "libx264",
                        "-crf",
                        "28",
                        "-preset",
                        "fast",
                        "-movflags",
                        "+faststart",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                    ]
                )
        else:  # "quality"
            ffmpeg_args.extend(["-vf", "scale='min(1920,iw)':-2"])
            if output_format == "webm_vp9":
                ffmpeg_args.extend(
                    [
                        "-c:v",
                        "libvpx-vp9",
                        "-crf",
                        "30",
                        "-b:v",
                        "0",
                        "-cpu-used",
                        "2",
                        "-c:a",
                        "libopus",
                        "-b:a",
                        "128k",
                    ]
                )
            elif output_format == "webm_av1":
                ffmpeg_args.extend(
                    [
                        "-c:v",
                        "libsvtav1",
                        "-preset",
                        "6",
                        "-crf",
                        "35",
                        "-c:a",
                        "libopus",
                        "-b:a",
                        "128k",
                    ]
                )
            else:
                ffmpeg_args.extend(
                    [
                        "-c:v",
                        "libx264",
                        "-crf",
                        "23",
                        "-preset",
                        "medium",
                        "-movflags",
                        "+faststart",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                    ]
                )

        ffmpeg_args.append(output_path)

        process = await asyncio.create_subprocess_exec(
            *ffmpeg_args,
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

        obj = await upload_file(output_data, output_key, content_type)

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
            width=width,
            height=height,
            content_type=content_type,
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

        # Push completion to the client's callback (if any) on its own task, so
        # they don't have to poll GET /tasks/{id} and a slow receiver can't block
        # this worker slot. Best-effort; never affects the task result.
        await _enqueue_webhook(record, "video.completed")

        # Thin task: return ONLY execution state, never domain data. Everything a
        # client needs (download_url, size, duration_seconds, truncated, ...)
        # lives on the `uploads` row we just marked ready and is served *live* by
        # GET /files/{upload_id}. The TaskIQ result backend is immutable + TTL'd
        # (and a presigned url would even expire inside it), so a snapshot sealed
        # here becomes a stale second source of truth -- the split-brain we
        # deliberately avoid. upload_id is the pointer clients use to fetch the
        # authoritative record once this task reports completion.
        return {"status": "success", "upload_id": upload_id}

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
        # Only the (small) compressed output is a temp file now -- the input was
        # read in place (local path or presigned URL), never copied to /tmp.
        if await aiofiles.os.path.exists(output_path):
            await aiofiles.os.remove(output_path)

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
    duration, _, _ = await _probe_video_metadata(input_path)
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
    frame_path = f"/tmp/{unique_id}_poster.png"

    try:
        # Read the compressed video in place (local path or presigned URL) rather
        # than downloading it into RAM first.
        input_source = await _resolve_ffmpeg_input(src_key)

        seek = at_seconds if at_seconds is not None else await _poster_seek_seconds(input_source)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-ss",
            f"{seek:.3f}",
            "-i",
            input_source,
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
        if await aiofiles.os.path.exists(frame_path):
            await aiofiles.os.remove(frame_path)


@broker.task(schedule=[{"cron": "0 * * * *"}])
async def cleanup_orphaned_uploads_task(hours: int = 24, batch_size: int = 100) -> dict:
    """Cron-based cleanup mechanism that automatically deletes unlinked uploads older than 24 hours.

    Fetches unlinked records older than `hours` in batches. For each record:
    1. Attempts to delete physical file via `delete_file(storage_key)`.
    2. On success (or if missing/404), deletes the database record via `store.delete(upload_id)`.
    """
    store = await get_metadata_store()
    total_deleted = 0
    total_failed = 0

    while True:
        try:
            records = await store.get_unlinked_older_than(hours=hours, limit=batch_size)
        except MetadataError as exc:
            logger.error("Failed to fetch unlinked uploads for cleanup: %s", exc)
            break

        if not records:
            break

        for record in records:
            storage_ok = False
            try:
                await delete_file(record.storage_key)
                storage_ok = True
            except StorageError as exc:
                err_str = str(exc).lower()
                if "not found" in err_str or "nosuchkey" in err_str or "404" in err_str:
                    logger.info(
                        "Storage key %s missing during cleanup; proceeding to remove record",
                        record.storage_key,
                    )
                    storage_ok = True
                else:
                    logger.error(
                        "Failed to delete physical file %s during cleanup: %s",
                        record.storage_key,
                        exc,
                    )
                    total_failed += 1

            if storage_ok:
                try:
                    await store.delete(record.id)
                    total_deleted += 1
                except MetadataError as exc:
                    logger.error("Failed to delete record %s during cleanup: %s", record.id, exc)
                    total_failed += 1

    return {"status": "success", "deleted": total_deleted, "failed": total_failed}
