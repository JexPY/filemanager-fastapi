import os
import copy
import _thread
from fastapi import HTTPException,status

from .helpers.alena import local_savings
from .images.generateQr import qr_code_image
from .storage.googleCloud import upload_image_file_to_google_storage
from .storage.s3 import upload_image_file_to_s3_storage

def handle_qr_code(text = str, with_logo = bool):
    try:
        local_savings(qrCodes=True)
        
        qrCodePaths = qr_code_image(text, with_logo)

        if os.environ.get('PREFERED_STORAGE') == 'google':
            _thread.start_new_thread(upload_image_file_to_google_storage, (copy.deepcopy(qrCodePaths),))
            qrCodePaths['qrImage'] = os.environ.get('GOOGLE_BUCKET_URL') + os.environ.get('QR_IMAGE_GOOGLE_CLOUD_PATH') + qrCodePaths['qrImage'] if qrCodePaths.get('qrImage') else None

        elif os.environ.get('PREFERED_STORAGE') == 's3':
            _thread.start_new_thread(upload_image_file_to_s3_storage, (copy.deepcopy(qrCodePaths),))
            qrCodePaths['qrImage'] = os.environ.get('AWS_BUCKET_URL') + os.environ.get('QR_IMAGE_S3_CLOUD_PATH') + qrCodePaths['qrImage'] if qrCodePaths.get('qrImage') else None

        elif os.environ.get('PREFERED_STORAGE') == 'local':
            qrCodePaths['qrImage'] = os.environ.get('API_URL') + os.environ.get('QR_IMAGE_LOCAL_PATH') + qrCodePaths['qrImage'] if qrCodePaths.get('qrImage') else None

        qrCodePaths['storage'] = os.environ.get('PREFERED_STORAGE')
        return qrCodePaths
    except:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='The file format not supported')