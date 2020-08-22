import os
from libcloud.storage.types import Provider
from libcloud.storage.providers import get_driver
from ..helpers.alena import cleaning_service

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
        return cleaning_service(videoPaths, videos=True)

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
        if imagePaths.get('qrImage'):
                with open('./' + os.environ.get('QR_IMAGE_LOCAL_PATH') + imagePaths['qrImage'], 'rb') as iterator:
                        googleCloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('QR_IMAGE_GOOGLE_CLOUD_PATH') + imagePaths['qrImage'])
        return cleaning_service(imagePaths, images=True)