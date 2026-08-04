import base64
import hashlib
import hmac

from app.config import settings


def sign_url(path: str) -> str:
    key = bytes.fromhex(settings.IMGPROXY_KEY)
    salt = bytes.fromhex(settings.IMGPROXY_SALT)

    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(salt)
    mac.update(path.encode())
    signature = base64.urlsafe_b64encode(mac.digest()).decode("utf-8").rstrip("=")

    return f"/{signature}{path}"


def generate_signed_url(url: str, processing_options: str = "rs:fill:300:300") -> str:
    encoded_url = base64.urlsafe_b64encode(url.encode()).decode("utf-8").rstrip("=")
    path = f"/{processing_options}/{encoded_url}"
    return sign_url(path)
