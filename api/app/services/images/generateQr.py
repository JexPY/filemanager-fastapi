import os
import qrcode
from PIL import Image
from pathlib import Path
from ..helpers.uniqueFileName import generate_unique_name


def qr_code_image(text = str, with_logo = bool):
    print(with_logo)
    qrImagePIL = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        border=2,
    )

    qrImagePIL.add_data(text)

    qrImagePIL.make()

    qrImage = qrImagePIL.make_image().convert('RGB')
    if with_logo:
        logo = Image.open(os.environ.get('QR_IMAGE_LOGO_PATH'))
        qrImage.paste(logo, ((qrImage.size[0] - logo.size[0]) // 2, (qrImage.size[1] - logo.size[1]) // 2))
    
    qrUniqueName = generate_unique_name('png')[0]
    qrImage.save(os.environ.get('QR_IMAGE_LOCAL_PATH') + qrUniqueName)

    return {
        'qrImage' : qrUniqueName
    }
