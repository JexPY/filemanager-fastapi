import requests
import concurrent.futures
from typing import List
from fastapi import HTTPException,status,Query
from .serveUploadedFiles import handle_upload_image_file,handle_upload_video_file
import sys

def handle_download_data_from_url(url = str, thumbnail = bool, optimize = None, file_type = str):
    try:
        get_data = requests.get(url, stream = True)
        if get_data.status_code == 200:
            get_data.raw.decode_content = True
            if file_type == 'image':
                return handle_upload_image_file(thumbnail, upload_file=None, raw_data_file=get_data)
            elif file_type =='video':
                return handle_upload_video_file(optimize, upload_file=None, raw_data_file=get_data)
        else:
           raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f'Unsuccessful request for: {url}')
    except:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f'Unable to download data from: {url}')


def handle_multiple_image_file_downloads(URL_FILES: List[Query], workers: int, thumbnail = bool):
    # We can use a with statement to ensure threads are cleaned up promptly
    with concurrent.futures.ThreadPoolExecutor(max_workers = workers) as executor:

        future_to_url = {executor.submit(handle_download_data_from_url, eachFile, thumbnail, file_type = 'image'): eachFile for eachFile in URL_FILES}
        result = []
        for future in concurrent.futures.as_completed(future_to_url):
            try:
                result.append(future.result())
            except:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Multiple upload failed')
        return result