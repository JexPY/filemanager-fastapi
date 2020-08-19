import os
import ffmpeg
from PIL import Image
from uuid import uuid4
from pathlib import Path
from fastapi import HTTPException, status


def video_file_FFMPEG(temp_stored_file: Path, optimize: bool):
    try:
        local_savings()
        origin, optimized = generate_unique_name('mp4', 'mp4')
        # Save original
        if os.environ.get('SAVE_ORIGINAL') == 'True':
            (
                ffmpeg
                .input(temp_stored_file)
                .output(os.environ.get('VIDEO_ORIGINAL_PATH') + origin)
                .run()
            )
        else:
            origin = None
        if optimize:
            aud = ffmpeg.input(temp_stored_file).audio
            vid = ffmpeg.input(temp_stored_file).video.filter('scale', size='640x1136', force_original_aspect_ratio='decrease').filter('pad', '640', '1136', '(ow-iw)/2', '(oh-ih)/2')
            ffmpeg.concat(vid, aud, v=1, a=1)
            out = ffmpeg.output(vid, aud, os.environ.get('VIDEO_OPTIMIZED_PATH') + optimized)
            out.run()
        else:
            optimized = None
        return {
            'original': origin,
            'optimized': os.environ.get('VIDEO_OPTIMIZED_PATH') + optimized
        }
    except:
        raise HTTPException(status_code=503, detail="Image manipulation failed using FFMPEG")


def local_savings():
    Path(os.environ.get('VIDEO_ORIGINAL_PATH')).mkdir(parents=True, exist_ok=True)
    Path(os.environ.get('VIDEO_OPTIMIZED_PATH')).mkdir(parents=True, exist_ok=True)

def generate_unique_name(extension, desiredExtension):
    unique = uuid4().hex
    # First goes original, second is thumbnail with desiredExtension
    return unique + '.' +  extension, unique + '.' +  desiredExtension,