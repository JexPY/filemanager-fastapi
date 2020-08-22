import os
import ffmpeg
from PIL import Image
from pathlib import Path
from ..helpers.uniqueFileName import generate_unique_name
from ..helpers.alena import local_savings
from fastapi import HTTPException, status


def video_file_FFMPEG(temp_stored_file: Path, optimize: bool):
    try:
        if not optimize and not os.environ.get('SAVE_ORIGINAL') == 'True':
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Save original is dissabled, contact admin')
        
        local_savings(videos=True)
        origin, optimized = generate_unique_name(os.environ.get('VIDEO_AllOWED_FILE_FORMAT'), os.environ.get('VIDEO_DESIRED_FILE_FORMAT'))
        # Save original with config is ready for original file of mp4 or mov also decreases size by default
        if os.environ.get('SAVE_ORIGINAL') == 'True':
            (
                ffmpeg
                .input(temp_stored_file)
                .output(os.environ.get('VIDEO_ORIGINAL_LOCAL_PATH') + origin, vcodec='h264', acodec='aac')
                .run(quiet=True)
            )
        else:
            origin = None
        if optimize:
            audio = ffmpeg.input(temp_stored_file).audio
            video = ffmpeg.input(temp_stored_file).video.filter('scale', size='640x1136', force_original_aspect_ratio='decrease').filter('pad', '640', '1136', '(ow-iw)/2', '(oh-ih)/2')
            ffmpeg.concat(video, audio, v=1, a=1)
            # ffmpeg config for webm
            # Also is possible to use vcodec libvpx-vp9 but sometimes it increzes size needs testing may it suits you more
            # Check docs https://trac.ffmpeg.org/wiki/Encode/VP9
            if os.environ.get('VIDEO_DESIRED_FILE_FORMAT') == 'webm':
                out = ffmpeg.output(video, audio, os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH') + optimized, crf='10', qmin='0', qmax='50', video_bitrate='1M', vcodec='libvpx', acodec='libvorbis')
            # ffmpeg config for mp4
            elif os.environ.get('VIDEO_DESIRED_FILE_FORMAT') == 'mp4':
                out = ffmpeg.output(video, audio, os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH') + optimized, vcodec='h264', acodec='aac')
            out.run(quiet=True)
        else:
            optimized = None
        return {
            'original': origin,
            'optimized': optimized
        }
    except:
        raise HTTPException(status_code=503, detail="Video manipulation failed using FFMPEG")