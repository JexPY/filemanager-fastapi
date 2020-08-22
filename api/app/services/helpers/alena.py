'''
    Alena will manage directories
'''
import os
from pathlib import Path


def cleaning_service(pathsToClean, images = False, videos = False):
        if images:
                if pathsToClean.get('original'):
                        if Path(os.environ.get('IMAGE_ORIGINAL_LOCAL_PATH') + pathsToClean['original']).is_file():
                                Path(os.environ.get('IMAGE_ORIGINAL_LOCAL_PATH') + pathsToClean['original']).unlink()
                if pathsToClean.get('thumbnail'):
                        if Path(os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH') + pathsToClean['thumbnail']).is_file():
                                Path(os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH') + pathsToClean['thumbnail']).unlink()
                if pathsToClean.get('qrImage'):
                        if Path(os.environ.get('QR_IMAGE_LOCAL_PATH') + pathsToClean['qrImage']).is_file():
                                Path(os.environ.get('QR_IMAGE_LOCAL_PATH') + pathsToClean['qrImage']).unlink()
        elif videos:
                if pathsToClean.get('original'):
                        if Path(os.environ.get('VIDEO_ORIGINAL_LOCAL_PATH') + pathsToClean['original']).is_file():
                                Path(os.environ.get('VIDEO_ORIGINAL_LOCAL_PATH') + pathsToClean['original']).unlink()
                if pathsToClean.get('optimized'):
                        if Path(os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH') + pathsToClean['optimized']).is_file():
                                Path(os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH') + pathsToClean['optimized']).unlink()

def local_savings(images = False, videos = False, qrCodes = False):
    if images:
        Path(os.environ.get('IMAGE_ORIGINAL_LOCAL_PATH')).mkdir(parents=True, exist_ok=True)
        Path(os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH')).mkdir(parents=True, exist_ok=True)
    elif videos:
        Path(os.environ.get('VIDEO_ORIGINAL_LOCAL_PATH')).mkdir(parents=True, exist_ok=True)
        Path(os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH')).mkdir(parents=True, exist_ok=True)
    elif qrCodes:
        Path(os.environ.get('QR_IMAGE_LOCAL_PATH')).mkdir(parents=True, exist_ok=True)
