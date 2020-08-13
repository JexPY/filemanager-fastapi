import os
from pathlib import Path
from libcloud.storage.types import Provider
from libcloud.storage.providers import get_driver

def uploadFileToGoogleStorage(imagePaths: dict):
        cls = get_driver(Provider.GOOGLE_STORAGE)
        googleCloudStorageDriver = cls(os.environ.get('GOOGLE_CLIENT_EMAIL'), os.environ.get('GOOGLE_STORAGE_KEY_FILE'))

        container = googleCloudStorageDriver.get_container(os.environ.get('DEFAULT_BUCKET_NAME'))
        if imagePaths.get('original'):
                with open('./' + os.environ.get('IMAGE_ORIGINAL_PATH') + imagePaths['original'], 'rb') as iterator:
                        googleCloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('IMAGE_ORIGINAL_PATH') + imagePaths['original'])
        if imagePaths.get('thumbnail'):
                with open('./' + os.environ.get('IMAGE_THUMBNAIL_PATH') + imagePaths['thumbnail'], 'rb') as iterator:
                        googleCloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('IMAGE_THUMBNAIL_PATH') + imagePaths['thumbnail']) 
        return cleanAfterYourself(imagePaths)

def cleanAfterYourself(imagePaths):
        if imagePaths.get('original'):
                if Path(os.environ.get('IMAGE_ORIGINAL_PATH') + imagePaths['original']).is_file():
                        Path(os.environ.get('IMAGE_ORIGINAL_PATH') + imagePaths['original']).unlink()
        if imagePaths.get('original'):
                if Path(os.environ.get('IMAGE_THUMBNAIL_PATH') + imagePaths['thumbnail']).is_file():
                        Path(os.environ.get('IMAGE_THUMBNAIL_PATH') + imagePaths['thumbnail']).unlink()