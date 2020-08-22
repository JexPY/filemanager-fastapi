import os
from libcloud.storage.types import Provider
from libcloud.storage.providers import get_driver
from ..helpers.alena import cleaning_service

def authorize_aws_s3():
        cls = get_driver(Provider.S3)
        awsS3storageDriver = cls(
                os.environ.get('AWS_ACCESS_KEY_ID'), 
                os.environ.get('AWS_SECRET_ACCESS_KEY'), 
                region = os.environ.get('AWS_DEFAULT_REGION')
                )

        s3Bucket= awsS3storageDriver.get_container(container_name=os.environ.get('AWS_BUCKET'))
        extra = {'content_type': 'application/octet-stream'}
                
        return awsS3storageDriver, s3Bucket, extra


def upload_video_file_to_s3_storage(videoPaths: dict):
        awsS3cloudStorageDriver, container, extra = authorize_aws_s3()

        if videoPaths.get('original'):
                with open('./' + os.environ.get('VIDEO_ORIGINAL_LOCAL_PATH') + videoPaths['original'], 'rb') as iterator:
                        awsS3cloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('VIDEO_ORIGINAL_S3_CLOUD_PATH') + videoPaths['original'],
                                                                extra=extra)
        if videoPaths.get('optimized'):
                with open('./' + os.environ.get('VIDEO_OPTIMIZED_LOCAL_PATH') + videoPaths['optimized'], 'rb') as iterator:
                        awsS3cloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('VIDEO_OPTIMIZED_S3_CLOUD_PATH') + videoPaths['optimized'],
                                                                extra=extra)
        return cleaning_service(videoPaths, videos=True)

def upload_image_file_to_s3_storage(imagePaths: dict):
        awsS3cloudStorageDriver, container, extra = authorize_aws_s3()

        if imagePaths.get('original'):
                with open('./' + os.environ.get('IMAGE_ORIGINAL_LOCAL_PATH') + imagePaths['original'], 'rb') as iterator:
                        awsS3cloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('IMAGE_ORIGINAL_S3_CLOUD_PATH') + imagePaths['original'],
                                                                extra=extra)
        if imagePaths.get('thumbnail'):
                with open('./' + os.environ.get('IMAGE_THUMBNAIL_LOCAL_PATH') + imagePaths['thumbnail'], 'rb') as iterator:
                        awsS3cloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('IMAGE_THUMBNAIL_S3_CLOUD_PATH') + imagePaths['thumbnail'],
                                                                extra=extra)
        if imagePaths.get('qrImage'):
                with open('./' + os.environ.get('QR_IMAGE_LOCAL_PATH') + imagePaths['qrImage'], 'rb') as iterator:
                        awsS3cloudStorageDriver.upload_object_via_stream(iterator=iterator,
                                                                container=container,
                                                                object_name=os.environ.get('QR_IMAGE_S3_CLOUD_PATH') + imagePaths['qrImage'],
                                                                extra=extra)
        return cleaning_service(imagePaths, images=True)