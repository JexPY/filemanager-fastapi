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

    signed_path = f"/{signature}{path}"
    # The signature covers only the path (per imgproxy's spec) -- the base
    # URL is prefixed after signing, purely so callers get a complete,
    # fetchable URL instead of a path they'd need external knowledge to use.
    base = settings.IMGPROXY_BASE_URL.rstrip("/")
    return f"{base}{signed_path}" if base else signed_path


def generate_signed_url(
    url: str, processing_options: str = "rs:fill:300:300", format: str | None = None
) -> str:
    encoded_url = base64.urlsafe_b64encode(url.encode()).decode("utf-8").rstrip("=")
    path = f"/{processing_options}/{encoded_url}"
    if format:
        path = f"{path}.{format}"
    return sign_url(path)


def build_source_url(key: str, url: str) -> str:
    """The source imgproxy should fetch from for a given stored object.

    With STORAGE_BACKEND=local, imgproxy (a separate container) has no HTTP
    path to the API's storage at all -- `url` would be an unreachable bare
    key or localhost address. It does share the same media_data volume
    (mounted read-only, with IMGPROXY_LOCAL_FILESYSTEM_ROOT pointing at it
    in docker-compose.yml), so local objects use imgproxy's local:// source
    scheme instead. Every other backend already returns a URL imgproxy can
    fetch directly (a presigned or public object URL), so `url` is used
    as-is.
    """
    if settings.STORAGE_BACKEND == "local":
        return f"local:///{key}"
    return url
