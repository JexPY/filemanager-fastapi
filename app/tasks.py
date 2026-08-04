import asyncio
import logging
import os
import uuid

import aiofiles
import aiofiles.os

from app.broker import broker
from app.config import settings
from app.services.storage import StorageError, delete_file, download_file, upload_file

logger = logging.getLogger(__name__)


@broker.task
async def compress_video_task(raw_storage_key: str, original_filename: str) -> dict:
    unique_id = uuid.uuid4().hex
    input_path = f"/tmp/{unique_id}_{os.path.basename(raw_storage_key)}"
    output_path = f"/tmp/{unique_id}_compressed.mp4"
    output_key = f"videos/{unique_id}_compressed.mp4"

    try:
        # Pull the raw upload from storage (only its key travels through Redis).
        raw_data = await download_file(raw_storage_key)
        async with aiofiles.open(input_path, "wb") as f:
            await f.write(raw_data)

        # Run FFmpeg 7 async: H.264/AAC, cap output at 60s, -crf 28, -preset fast.
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-t",
            "60",
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

        return {
            "status": "success",
            "url": obj.url,
            "key": obj.key,
            "size": obj.size,
            "original_filename": original_filename,
        }

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
