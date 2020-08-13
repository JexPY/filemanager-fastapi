import os
import shutil
import imghdr
import _thread
import copy
from typing import List
from pathlib import Path
import concurrent.futures
from .images.resize import resize_image
from tempfile import NamedTemporaryFile
from fastapi import UploadFile,HTTPException,status
from .storage.googleCloud import uploadFileToGoogleStorage


def save_upload_file_tmp(upload_file: UploadFile) -> Path:
    try:
        suffix = Path(upload_file.filename).suffix
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(upload_file.file, tmp)
            tmp_path = Path(tmp.name)
    except:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Impossible to manipulate the file')
    finally:
        upload_file.file.close()
    return tmp_path


def handle_upload_image_file(thumbnail, upload_file: UploadFile):
    try:
        tmp_path = save_upload_file_tmp(upload_file)
        if imghdr.what(tmp_path):

            imagePaths = resize_image(tmp_path, imghdr.what(tmp_path), thumbnail, os.environ.get('IMAGE_CONVERTING_PREFERED_FORMAT'))
            
            if os.environ.get('PREFERED_STORAGE') == 'google':
                _thread.start_new_thread(uploadFileToGoogleStorage, (copy.deepcopy(imagePaths),))
                imagePaths['original'] = os.environ.get('GOOGLE_BUCKET_URL') + os.environ.get('IMAGE_ORIGINAL_PATH') + imagePaths['original'] if imagePaths.get('original') else imagePaths.get('original')
                imagePaths['thumbnail'] = os.environ.get('GOOGLE_BUCKET_URL') + os.environ.get('IMAGE_THUMBNAIL_PATH') + imagePaths['thumbnail'] if imagePaths.get('thumbnail') else imagePaths.get('thumbnail')

            imagePaths['storage'] = os.environ.get('PREFERED_STORAGE')
            return imagePaths
        else:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='The file format not supported')
    finally:
        tmp_path.unlink()  # Delete the temp file


def handle_multiple_image_file_uploads(FILES: List[UploadFile], workers: int):
    # We can use a with statement to ensure threads are cleaned up promptly
    with concurrent.futures.ThreadPoolExecutor(max_workers = workers) as executor:
        # Start the load operations and mark each future with its FILES thumbnail is default for this function
        future_to_url = {executor.submit(handle_upload_image_file, True, eachFile): eachFile for eachFile in FILES}
        result = []
        for future in concurrent.futures.as_completed(future_to_url):
            # try:
                result.append(future.result())
            # except:
            #     raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Multiple upload failed')
        return result