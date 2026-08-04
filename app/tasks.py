import asyncio
import os
import uuid

import aiofiles

from app.broker import broker
from app.services.storage import download_file, upload_file


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

        # Run FFmpeg 7 async: H.264/AAC, cap at 60s, -crf 28, -preset fast.
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", input_path,
            "-t", "60",
            "-c:v", "libx264", "-crf", "28", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

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
            if os.path.exists(path):
                os.remove(path)
