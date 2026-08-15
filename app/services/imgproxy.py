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


def public_source_url(key: str) -> str:
    """The **durable** source URL for a stored object -- the only one safe to
    embed in an imgproxy URL that is handed out as permanent.

    Always the plain object URL, never a presigned one. That distinction is the
    whole point: an imgproxy signature never expires, so embedding a presigned
    source produces a URL that *looks* permanent and silently dies when the
    signature does. The upload response used to build its URLs that way while
    ``UploadRecord.to_public`` used the plain object URL, so the same image had
    two different imgproxy URLs -- two CDN cache keys, one of them expiring.

    Requires imgproxy to be able to fetch that URL: a public bucket or a CDN in
    front of it (``*_PUBLIC_BASE_URL``). Private objects do not get a durable
    imgproxy URL at all -- by design, since a permanent imgproxy URL is a
    permanent bearer capability with no owner check.
    """
    if settings.STORAGE_BACKEND == "local":
        # imgproxy reads the shared volume via its local:// scheme; there is no
        # HTTP source URL to build, so skip resolving a backend entirely.
        return build_source_url(key, "")
    from app.services.storage import public_object_url

    return build_source_url(key, public_object_url(key))


def signed_image_url(
    key: str, processing_options: str = "rs:auto", format: str | None = None
) -> str:
    """The one way to build an imgproxy URL for a stored object.

    Every caller goes through here so the source-URL derivation cannot drift
    apart again (see ``public_source_url``). Sync on purpose -- ``to_public`` is
    sync, and resolving a durable source needs no I/O.
    """
    return generate_signed_url(
        public_source_url(key), processing_options=processing_options, format=format
    )
