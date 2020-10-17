import os
import shutil
import _thread
import copy
from typing import List
from pathlib import Path
import concurrent.futures
from .images.resize import resize_image
from .videos.optimize import video_file_FFMPEG
from tempfile import NamedTemporaryFile
from fastapi import UploadFile,HTTPException,status
from .storage.googleCloud import upload_image_file_to_google_storage,upload_video_file_to_google_storage
from .storage.s3 import upload_image_file_to_s3_storage,upload_video_file_to_s3_storage
from .helpers.detectFileExtension import magic_extensions
import sys

def save_upload_file_tmp(upload_file: None, raw_data_file = None) -> Path:
    try:
        if raw_data_file:
            with NamedTemporaryFile(delete=False, suffix=None) as tmp:
                    shutil.copyfileobj(raw_data_file.raw, tmp)

        else:
            with NamedTemporaryFile(delete=False, suffix=None) as tmp:
                shutil.copyfileobj(upload_file.file, tmp)

        extension = magic_extensions(Path(tmp.name))
        final_temp_file = tmp.name + extension
        os.rename(Path(tmp.name), final_temp_file)

    except:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Impossible to manipulate the file')
    finally:
        if upload_file:
            upload_file.file.close()
        else:
            raw_data_file.close()
    return Path(final_temp_file), extension


def handle_upload_image_file(thumbnail, upload_file: None, raw_data_file = None):
    try:
        tmp_path, file_extension = save_upload_file_tmp(upload_file, raw_data_file)

        if file_extension[1:] in os.environ.get('IMAGE_AllOWED_FILE_FORMAT').split(','):

            imagePaths = resize_image(tmp_path, file_extension[1:], thumbnail, os.environ.get('IMAGE_CONVERTING_PREFERED_FORMAT'))
            
            if os.environ.get('PREFERED_STORAGE') == 'google':
                _thread.start_new_thread(upload_image_file_to_google_storage, (copy.deepcopy(imagePaths),))
                imagePaths['original'] = os.environ.get('GOOGLE_BUCKET_URL') + os.environ.get('IMAGE_ORIGINAL_GOOGLE_CLOUD_PATH') + imagePaths['original'] if imagePaths.get('original') else None
                imagePaths['thumbnail'] = os.environ.get('GOOGLE_BUCKET_URL') + os.environ.get('IMAGE_THUMBNAIL_GOOGLE_CLOUD_PATH') + imagePaths['thumbnail'] if imagePaths.get('thumbnail') else None

            elif os.environ.get('PREFERED_STORAGE') == 's3':
                _thread.start_new_thread(upload_image_file_to_s3_storage, (copy.deepcopy(imagePaths),))
                imagePaths['original'] = os.environ.get('AWS_BUCKET_URL') + os.environ.get('IMAGE_ORIGINAL_S3_CLOUD_PATH') + imagePaths['original'] if imagePaths.get('original') else None
                imagePaths['thumbnail'] = os.environ.get('AWS_BUCKET_URL') + os.environ.get('IMAGE_THUMBNAIL_S3_CLOUD_PATH') + imagePaths['thumbnail'] if imagePaths.get('thumbnail') else None

            elif os.environ.get('PREFERED_STORAGE') == 'local':
                imagePaths['original'] = os.environ.get('API_URL') + os.environ.get('IMAGE_ORIGINAL_LOCAL_PATH') + imagePaths['original'] if imagePaths.get('original') else None
                imagePaths['thumbnail'] = os.environ.get('API_URL') + os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH') + imagePaths['thumbnail'] if imagePaths.get('thumbnail') else None

            imagePaths['storage'] = os.environ.get('PREFERED_STORAGE')
            imagePaths['file_name'] = imagePaths['original'].split('/')[-1]
            return imagePaths
        else:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='The file format not supported')
    finally:
        tmp_path.unlink()  # Delete the temp file


def handle_multiple_image_file_uploads(FILES: List[UploadFile], workers: int, thumbnail: bool):
    # We can use a with statement to ensure threads are cleaned up promptly
    with concurrent.futures.ThreadPoolExecutor(max_workers = workers) as executor:
        future_to_url = {executor.submit(handle_upload_image_file, eachFile, thumbnail): eachFile for eachFile in FILES}
        result = []
        for future in concurrent.futures.as_completed(future_to_url):
            try:
                result.append(future.result())
            except:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Multiple upload failed')
        return result


def handle_upload_video_file(optimize, upload_file: None, raw_data_file = None):

    try:
        tmp_path, file_extension = save_upload_file_tmp(upload_file, raw_data_file)

        if file_extension[1:] in os.environ.get('VIDEO_AllOWED_FILE_FORMAT').split(','):
                videoPaths = video_file_FFMPEG(tmp_path, optimize)
                
                if os.environ.get('PREFERED_STORAGE') == 'google':

                    _thread.start_new_thread(upload_video_file_to_google_storage, (copy.deepcopy(videoPaths),))
                    videoPaths['original'] = os.environ.get('GOOGLE_BUCKET_URL') + os.environ.get('VIDEO_ORIGINAL_GOOGLE_CLOUD_PATH') + videoPaths['original'] if videoPaths.get('original') else None
                    videoPaths['optimized'] = os.environ.get('GOOGLE_BUCKET_URL') + os.environ.get('VIDEO_OPTIMIZED_GOOGLE_CLOUD_PATH') + videoPaths['optimized'] if videoPaths.get('optimized') else None

                elif os.environ.get('PREFERED_STORAGE') == 's3':

                    _thread.start_new_thread(upload_video_file_to_s3_storage, (copy.deepcopy(videoPaths),))
                    videoPaths['original'] = os.environ.get('AWS_BUCKET_URL') + os.environ.get('VIDEO_ORIGINAL_S3_CLOUD_PATH') + videoPaths['original'] if videoPaths.get('original') else None
                    videoPaths['optimized'] = os.environ.get('AWS_BUCKET_URL') + os.environ.get('VIDEO_OPTIMIZED_S3_CLOUD_PATH') + videoPaths['optimized'] if videoPaths.get('optimized') else None

                elif os.environ.get('PREFERED_STORAGE') == 'local':
                    videoPaths['original'] = os.environ.get('API_URL') + os.environ.get('VIDEO_ORIGINAL_LOCAL_PATH') + videoPaths['original'] if videoPaths.get('original') else None
                    videoPaths['optimized'] = os.environ.get('API_URL') + os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH') + videoPaths['optimized'] if videoPaths.get('optimized') else None

                videoPaths['storage'] = os.environ.get('PREFERED_STORAGE')
                videoPaths['file_name'] = videoPaths['original'].split('/')[-1]

                return videoPaths
        else:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Not valid format')
    except:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Corrupted file')

    finally:
        tmp_path.unlink()  # Delete the temp file