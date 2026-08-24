import asyncio
import contextlib
import json
import logging
import os
import tempfile
import uuid
from typing import Any

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
from app.services.storage import (
    StorageError,
    delete_file,
    get_storage,
    storage_prefix,
    upload_file,
    upload_file_from_path,
)
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
    local = await backend.local_path(storage_key)
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
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=settings.FFPROBE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # TimeoutError is a subclass of OSError, so without this it would
            # fall through to the `except (ValueError, OSError)` below and be
            # treated as an ordinary probe failure -- silently leaking this
            # process (it's never killed/reaped), unlike every ffmpeg call
            # site in this file, which explicitly kill()+wait() on timeout.
            process.kill()
            await process.wait()
            logger.warning(
                "ffprobe timed out after %ss for %s",
                settings.FFPROBE_TIMEOUT_SECONDS,
                input_path,
            )
            return None, None, None
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
    except (ValueError, OSError) as exc:
        logger.warning("ffprobe could not determine metadata for %s: %s", input_path, exc)
        return None, None, None


def _effective_output_duration(
    input_duration: float | None,
    start_seconds: float | None,
    end_seconds: float | None,
) -> float | None:
    """Seconds of source the encode will actually produce *before* the
    VIDEO_MAX_DURATION_SECONDS cap applies -- the requested trim window
    intersected with what the input really contains. Pure function, so the
    truncation verdict is testable without running ffmpeg.

    None means the duration couldn't be probed; the caller then reports
    truncation as false (unknown) rather than guessing.

    The clamp against the input is the point: a caller that passes a
    deliberately large `end_seconds` to mean "through to the end" would
    otherwise look like a multi-minute request and get flagged `truncated` even
    though the whole (short) clip was encoded intact.
    """
    if input_duration is None:
        return None
    start = start_seconds or 0.0
    available = max(0.0, input_duration - start)
    if end_seconds is None:
        return available
    requested = max(0.0, end_seconds - start)
    return min(requested, available)


def _build_ffmpeg_args(
    input_source: str,
    output_path: str,
    output_format: str,
    optimization: str,
    start_seconds: float | None,
    end_seconds: float | None,
    max_duration_seconds: float | None = None,
) -> list[str]:
    """Build the full ffmpeg argument list for video compression.
    Pure function: no I/O, no state. Testable in isolation."""
    ffmpeg_args = ["ffmpeg", "-y"]
    if start_seconds is not None:
        ffmpeg_args.extend(["-ss", str(start_seconds)])
    if end_seconds is not None:
        ffmpeg_args.extend(["-to", str(end_seconds)])

    ffmpeg_args.extend(["-i", input_source])

    if max_duration_seconds is not None and max_duration_seconds > 0:
        ffmpeg_args.extend(["-t", str(max_duration_seconds)])

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
    return ffmpeg_args


async def _extract_and_store_poster(
    unique_id: str,
    input_source: str,
    seek_seconds: float,
    owner: str,
    upload_id: str,
    visibility: str = "public",
) -> UploadRecord | None:
    """Extract one frame from input_source at seek_seconds, process through the
    image pipeline (validate/strip -> WebP), store it, and link it to upload_id.
    Returns the linked video UploadRecord on success, None if the video was
    deleted mid-generation (mirrors compress_video_task's mark_ready check).
    Caller owns cleanup of input_source; this function cleans its own temp frame.
    Raises RuntimeError on ffmpeg failure or TimeoutError on timeout."""
    store = await get_metadata_store()
    frame_path = os.path.join(tempfile.gettempdir(), f"{unique_id}_poster.png")
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-ss",
            f"{seek_seconds:.3f}",
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
        # generate_renditions=False: a poster never gets its own thumbnail/
        # medium rendition (see CLAUDE.md), so skip that encode work entirely
        # instead of throwing the result away.
        processed = await asyncio.to_thread(
            validate_and_strip_image, frame_bytes, generate_renditions=False
        )
        prefix = storage_prefix("posters", visibility)
        poster_key = f"{prefix}/{unique_id}.webp"
        obj = await upload_file(processed.buffer, poster_key, processed.content_type)

        try:
            poster = await store.create(
                owner=owner,
                kind=KIND_IMAGE,
                storage_key=obj.key,
                content_type=processed.content_type,
                size_bytes=obj.size,
                status=STATUS_READY,
                width=processed.width,
                height=processed.height,
                visibility=visibility,
            )
        except MetadataError:
            # The object already exists in storage; a raised (not just a
            # failed-to-find-a-row) failure here must not leave it orphaned.
            with contextlib.suppress(StorageError):
                await delete_file(obj.key)
            raise

        # Link the video to its poster. None means the owner DELETEd the video
        # mid-generation -- discard the poster we just wrote (object + record)
        # instead of orphaning it. A *raised* MetadataError needs the same
        # cleanup: the object and the row we just created both already exist.
        try:
            linked = await store.set_poster(upload_id, poster.id)
        except MetadataError:
            with contextlib.suppress(StorageError):
                await delete_file(obj.key)
            with contextlib.suppress(MetadataError):
                await store.delete(poster.id, owner)
            raise
        if linked is None:
            logger.warning(
                "Upload %s gone during poster generation; discarding %s", upload_id, obj.key
            )
            with contextlib.suppress(StorageError):
                await delete_file(obj.key)
            with contextlib.suppress(MetadataError):
                await store.delete(poster.id, owner)
            return None

        return linked
    finally:
        if await aiofiles.os.path.exists(frame_path):
            await aiofiles.os.remove(frame_path)


async def _run_ffmpeg_compression(ffmpeg_args: list[str]) -> None:
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
        process.kill()
        await process.wait()
        raise RuntimeError(f"FFmpeg timed out after {settings.FFMPEG_TIMEOUT_SECONDS}s") from None

    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {stderr.decode(errors='replace')}")


async def _handle_optional_poster(
    unique_id: str,
    output_path: str,
    poster_seconds: float | None,
    record: UploadRecord,
    upload_id: str,
) -> UploadRecord:
    if poster_seconds is None:
        return record
    try:
        linked = await _extract_and_store_poster(
            unique_id,
            output_path,
            poster_seconds,
            record.owner,
            upload_id,
            visibility=record.visibility,
        )
        if linked is not None:
            return linked
    except Exception as p_exc:
        logger.warning("Automated poster extraction failed for %s: %s", upload_id, p_exc)
    return record


async def _cleanup_video_task_artifacts(output_path: str, raw_storage_key: str) -> None:
    if await aiofiles.os.path.exists(output_path):
        await aiofiles.os.remove(output_path)

    try:
        await delete_file(raw_storage_key)
    except StorageError as exc:
        logger.warning("Failed to delete raw video %s after processing: %s", raw_storage_key, exc)


@broker.task
async def compress_video_task(
    raw_storage_key: str,
    original_filename: str,
    upload_id: str,
    output_format: str = "mp4",
    optimization: str = "balanced",
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    poster_seconds: float | None = None,
) -> dict:
    unique_id = uuid.uuid4().hex

    store = await get_metadata_store()
    rec = await store.get_by_id(upload_id)
    visibility = rec.visibility if rec else "public"
    prefix = storage_prefix("videos", visibility)

    ext = "webm" if output_format.startswith("webm") else "mp4"
    content_type = f"video/{ext}"
    output_path = os.path.join(tempfile.gettempdir(), f"{unique_id}_compressed.{ext}")
    output_key = f"{prefix}/{unique_id}_compressed.{ext}"

    try:
        input_source = await _resolve_ffmpeg_input(raw_storage_key)
        input_duration, width, height = await _probe_video_metadata(input_source)

        requested_duration = _effective_output_duration(input_duration, start_seconds, end_seconds)
        truncated = bool(
            requested_duration is not None
            and settings.VIDEO_MAX_DURATION_SECONDS
            and requested_duration > settings.VIDEO_MAX_DURATION_SECONDS
        )

        ffmpeg_args = _build_ffmpeg_args(
            input_source,
            output_path,
            output_format,
            optimization,
            start_seconds,
            end_seconds,
            max_duration_seconds=settings.VIDEO_MAX_DURATION_SECONDS,
        )
        await _run_ffmpeg_compression(ffmpeg_args)

        obj = await upload_file_from_path(output_path, output_key, content_type)

        try:
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
        except MetadataError:
            with contextlib.suppress(StorageError):
                await delete_file(obj.key)
            raise
        if record is None:
            logger.warning(
                "Upload %s no longer exists (deleted mid-compression); discarding %s",
                upload_id,
                obj.key,
            )
            with contextlib.suppress(StorageError):
                await delete_file(obj.key)
            return {"status": "discarded", "upload_id": upload_id}

        record = await _handle_optional_poster(
            unique_id, output_path, poster_seconds, record, upload_id
        )

        await _enqueue_webhook(record, "video.completed")

        res: dict[str, Any] = {"status": "success", "upload_id": upload_id}
        if record.poster_upload_id:
            res["poster_id"] = record.poster_upload_id
        return res

    except Exception:
        failed_record: UploadRecord | None = None
        with contextlib.suppress(MetadataError):
            failed_record = await store.mark_failed(upload_id)
        if failed_record is not None:
            await _enqueue_webhook(failed_record, "video.failed")
        raise

    finally:
        await _cleanup_video_task_artifacts(output_path, raw_storage_key)


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

    # Read the compressed video in place (local path or presigned URL) rather
    # than downloading it into RAM first.
    input_source = await _resolve_ffmpeg_input(src_key)
    seek = at_seconds if at_seconds is not None else await _poster_seek_seconds(input_source)

    # Inherit the parent's visibility. Omitting it defaulted the poster to
    # `public`, so asking for a poster of a *private* video minted a publicly
    # fetchable still of it -- the inline poster path (compress_video_task) was
    # already passing this through; only the on-demand path was not.
    linked = await _extract_and_store_poster(
        unique_id, input_source, seek, video.owner, upload_id, visibility=video.visibility
    )
    if linked is None:
        return {"status": "discarded", "upload_id": upload_id}

    return {
        "status": "success",
        "upload_id": upload_id,
        "poster_id": linked.poster_upload_id,
    }
