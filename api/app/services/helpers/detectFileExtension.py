import magic
import mimetypes
from pathlib import Path
from fastapi import HTTPException,status

def magic_extensions(file_path: Path):
    mime = magic.Magic(mime=True)
    detectedExtension = mimetypes.guess_all_extensions(mime.from_buffer(open(file_path, "rb").read(2048)))
    if len(detectedExtension) > 0:
        return detectedExtension[0]
    else:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='File extension not detected')
