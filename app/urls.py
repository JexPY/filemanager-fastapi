"""App-relative -> absolute URL helper.

Lives at the top level (not in a router or service) so both the router layer
and the service layer (``UploadRecord.to_public``) can build public URLs without
one importing the other -- the service dataclass reaching into ``app.routers``
was an inverted dependency and a circular-import hazard.
"""

from app.config import settings


def public_url(path: str) -> str:
    """Turn an app-relative path (e.g. /share/<token>) into an absolute URL when
    PUBLIC_BASE_URL is configured, otherwise return the path unchanged so the
    client prefixes its own origin."""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}{path}" if base else path
