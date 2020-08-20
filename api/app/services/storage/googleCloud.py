import os
from pathlib import Path
from libcloud.storage.types import Provider
from libcloud.storage.providers import get_driver


def authorize_google():
        cls = get_driver(Provider.GOOGLE_STORAGE)
        googleStorageDriver = cls(os.environ.get('GOOGLE_CLIENT_EMAIL'), os.environ.get('GOOGLE_STORAGE_KEY_FILE'))

        googleContainer = googleStorageDriver.get_container(os.environ.get('DEFAULT_BUCKET_NAME'))
        return googleStorageDriver, googleContainer


def upload_video_file_to_google_storage(videoPaths: dict):
        googleCloudStorageDriver, container = authorize_google()

        if videoPaths.get('original'):
                with open('./' + os.environ.get('VIDEO_ORIGINAL_LOCAL_PATH') + videoPaths['original'], 'rb') as iterator:
                        googleCloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('VIDEO_ORIGINAL_GOOGLE_CLOUD_PATH') + videoPaths['original'])
        if videoPaths.get('optimized'):
                with open('./' + os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH') + videoPaths['optimized'], 'rb') as iterator:
                        googleCloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('VIDEO_OPTIMIZED_GOOGLE_CLOUD_PATH') + videoPaths['optimized']) 
        return clean_after_yourself(videoPaths, videos=True)

def upload_image_file_to_google_storage(imagePaths: dict):
        googleCloudStorageDriver, container = authorize_google()

        if imagePaths.get('original'):
                with open('./' + os.environ.get('IMAGE_ORIGINAL_LOCAL_PATH') + imagePaths['original'], 'rb') as iterator:
                        googleCloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('IMAGE_ORIGINAL_GOOGLE_CLOUD_PATH') + imagePaths['original'])
        if imagePaths.get('thumbnail'):
                with open('./' + os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH') + imagePaths['thumbnail'], 'rb') as iterator:
                        googleCloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('IMAGE_THUMBNAIL_GOOGLE_CLOUD_PATH') + imagePaths['thumbnail']) 
        return clean_after_yourself(imagePaths, images=True)



def clean_after_yourself(pathsToClean, images = False, videos = False):
        if images:
                if pathsToClean.get('original'):
                        if Path(os.environ.get('IMAGE_ORIGINAL_LOCAL_PATH') + pathsToClean['original']).is_file():
                                Path(os.environ.get('IMAGE_ORIGINAL_LOCAL_PATH') + pathsToClean['original']).unlink()
                if pathsToClean.get('thumbnail'):
                        if Path(os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH') + pathsToClean['thumbnail']).is_file():
                                Path(os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH') + pathsToClean['thumbnail']).unlink()
        if videos:
                if pathsToClean.get('original'):
                        if Path(os.environ.get('VIDEO_ORIGINAL_LOCAL_PATH') + pathsToClean['original']).is_file():
                                Path(os.environ.get('VIDEO_ORIGINAL_LOCAL_PATH') + pathsToClean['original']).unlink()
                if pathsToClean.get('optimized'):
                        if Path(os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH') + pathsToClean['optimized']).is_file():
                                Path(os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH') + pathsToClean['optimized']).unlink()