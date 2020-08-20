import os
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi import HTTPException,status


def response_image_file(filename:str, version:str):
    validPath = {
        'original': os.environ.get('IMAGE_ORIGINAL_PATH'),
        'thumbnail': os.environ.get('IMAGE_THUMBNAIL_PATH'),
        }

    if not Path(validPath[version] + filename).is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='File not found please recheck name')

    return FileResponse(validPath[version] + filename)
