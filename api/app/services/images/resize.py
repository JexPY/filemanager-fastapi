import os
from PIL import Image
from uuid import uuid4
from pathlib import Path
from fastapi import HTTPException


def resizeImage(temp_stored_file: Path, extension: str, desiredExtension: str):

    try:
        localSavings()
        resize_width = int(os.environ.get('THUMBNAIL_MAX_WIDHT'))
        infile = temp_stored_file
        unique = uuid4().hex
        origin = unique + '.' +  extension
        thumb = unique + '.' +  desiredExtension
        img = Image.open(infile)
        img.save(Path(os.environ.get('IMAGE_ORIGINAL_PATH') + origin).absolute())
        wpercent = (resize_width/float(img.size[0]))
        hsize = int((float(img.size[1])*float(wpercent)))
        img.thumbnail((resize_width,hsize), Image.BICUBIC)
        img.save(Path(os.environ.get('IMAGE_THUMBNAIL_PATH') + thumb).absolute())
        return {
            'original': origin,
            'thumbnail': thumb
        }
    except:
        raise HTTPException(status_code=503, detail="Image manipulation failed")

def localSavings():
    Path(os.environ.get('IMAGE_ORIGINAL_PATH')).mkdir(parents=True, exist_ok=True)
    Path(os.environ.get('IMAGE_THUMBNAIL_PATH')).mkdir(parents=True, exist_ok=True)