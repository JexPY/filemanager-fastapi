import os
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi import HTTPException,status


def response_image_file(filename:str, image_type:str):
    validPath = {
        'original': os.environ.get('IMAGE_ORIGINAL_LOCAL_PATH'),
        'thumbnail': os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH'),
        'qrImage': os.environ.get('QR_IMAGE_LOCAL_PATH'),
        }

    if not Path(validPath[image_type] + filename).is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='File not found please recheck name')

    return FileResponse(validPath[image_type] + filename)
