"""Bearer-token and capability-JWT authentication.

Two credential shapes are accepted on every protected route, resolved by a
single dual-auth path (`resolve_principal`):

* **Static master tokens** -- the entries in ``FILE_MANAGER_BEARER_TOKENS``
  (the original, still-supported behaviour). Full access, no scope restriction.
  This is the backend-to-backend secret, and the *only* credential allowed to
  mint capability JWTs (``POST /upload/presign``).
* **Capability JWTs** -- short-lived HS256 tokens signed with ``JWT_SECRET_KEY``
  carrying ``sub`` (owner id), ``exp`` (strictly enforced) and ``scopes`` (at
  least one of ``upload:image`` / ``upload:video``). A trusted backend mints one
  (directly, since the secret is shared, or via ``/upload/presign``) and hands
  it to an untrusted frontend, which then uploads *straight to this service* --
  the file bytes never round-trip the backend.

The token may arrive in the ``Authorization: Bearer <token>`` header or, for
clients that cannot set headers (plain ``<form>`` POSTs, third-party uploader
widgets, a ``<video src>``), as a ``?token=<jwt>`` query parameter -- the
"presigned URL" pattern. Both shapes flow through the exact same validation.

Keeping all of this in one module is deliberate (see CLAUDE.md): auth logic
lives here, not smeared across ``utils.py``.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.config import settings

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)
router = APIRouter()

# Capability scopes a JWT may carry. A usable upload JWT must grant at least one.
SCOPE_UPLOAD_IMAGE = "upload:image"
SCOPE_UPLOAD_VIDEO = "upload:video"
_UPLOAD_SCOPES = frozenset({SCOPE_UPLOAD_IMAGE, SCOPE_UPLOAD_VIDEO})


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. ``scopes is None`` marks a static master token,
    which is unrestricted; a JWT carries an explicit, finite scope set."""

    owner: str
    scopes: frozenset[str] | None = None

    def has_scope(self, scope: str) -> bool:
        # Static master tokens (scopes is None) bypass all scope checks.
        return self.scopes is None or scope in self.scopes


def _match_static_token(token: str) -> str | None:
    """Owner for a static bearer secret, or None. Compares against every
    configured secret in constant time (no early return on a match), the same
    way the original implementation did."""
    matched: str | None = None
    for secret, owner in settings.token_identities.items():
        if hmac.compare_digest(token, secret):
            matched = owner
    return matched


def _decode_capability_jwt(token: str) -> Principal | None:
    """Owner + scopes for a valid capability JWT, else None. Enforces ``exp`` and
    requires both ``sub`` and at least one upload scope. Never raises -- an
    expired, wrongly-signed, malformed, or under-scoped token is simply not a
    principal, and the caller turns that into the appropriate HTTP error."""
    secret = settings.JWT_SECRET_KEY
    if not secret:
        # JWT auth disabled: only static tokens are accepted.
        return None
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        # Bad signature, expired, missing required claim, malformed, ...
        logger.debug("Rejected JWT: %s", exc)
        return None

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    raw_scopes = payload.get("scopes")
    if not isinstance(raw_scopes, list) or not all(isinstance(s, str) for s in raw_scopes):
        return None
    scopes = frozenset(raw_scopes)
    if not (scopes & _UPLOAD_SCOPES):
        # A capability token that grants no upload scope is meaningless here.
        return None
    return Principal(owner=sub, scopes=scopes)


def resolve_principal(token: str | None) -> Principal | None:
    """Dual-auth core: resolve a raw bearer string to a Principal, or None.

    Static tokens are tried first (cheap, constant-time, the legacy path); an
    unmatched token is then tried as a capability JWT. Pure and side-effect
    free, so it is trivial to unit-test and mock -- the HTTP wrappers below are
    what turn a None into a status code.
    """
    if not token:
        return None
    owner = _match_static_token(token)
    if owner is not None:
        return Principal(owner=owner, scopes=None)
    return _decode_capability_jwt(token)


def _token_from_request(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> str | None:
    """The bearer string: the ``Authorization`` header if present, otherwise the
    ``?token=`` query parameter (the presigned-URL fallback for header-less
    clients). The header always wins when both are supplied."""
    if credentials is not None:
        return credentials.credentials
    return request.query_params.get("token") or None


def _authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None) -> Principal:
    principal = resolve_principal(_token_from_request(request, credentials))
    if principal is None:
        # Missing and invalid are both 401 -- don't hand out an oracle for which.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication token",
        )
    return principal


def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """FastAPI dependency: authenticate (static token or JWT; header or
    ``?token=``) and return the owner id. Backward compatible -- the return type
    and owner semantics are identical to the static-only implementation, so
    every existing ``owner: str = Depends(verify_token)`` route is unaffected."""
    return _authenticate(request, credentials).owner


def require_scopes(*required_scopes: str) -> Callable[..., str]:
    """Dependency factory: authenticate AND require the caller to hold at least
    one of ``required_scopes``. Static master tokens are unrestricted and always
    pass; a JWT that lacks a matching scope is a 403 (authenticated but not
    permitted). Returns the owner id, exactly like ``verify_token`` -- so it is a
    drop-in replacement on the routes that need a specific capability."""
    required = frozenset(required_scopes)

    def _dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> str:
        principal = _authenticate(request, credentials)
        if not any(principal.has_scope(scope) for scope in required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Token is missing a required scope ({', '.join(sorted(required))})",
            )
        return principal.owner

    return _dependency


def verify_static_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Master-token-only dependency: rejects capability JWTs. Guards the presign
    endpoint -- only a holder of the shared static secret may mint new upload
    capabilities, so a leaked JWT can never escalate into issuing more of them.
    401 if no credential, 403 if the credential is a (valid) non-static token."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    owner = _match_static_token(credentials.credentials)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a static master token",
        )
    return owner


def _resolve_owner_optional(
    credentials: HTTPAuthorizationCredentials | None,
    request: Request | None = None,
) -> str | None:
    """Owner for a presented, valid credential (static or JWT), else None -- it
    never raises. Used by the download endpoint (not the ``verify_token``
    dependency) because a ``public`` video is fetchable with no token at all,
    while a ``private`` one still needs its owner. Accepts the same header-or-
    ``?token=`` shapes as everything else (the query form matters here: a
    ``<video src>`` cannot set an Authorization header)."""
    token: str | None = None
    if credentials is not None:
        token = credentials.credentials
    elif request is not None:
        token = request.query_params.get("token") or None
    principal = resolve_principal(token)
    return principal.owner if principal is not None else None


class PresignedUploadRequest(BaseModel):
    """Body for ``POST /upload/presign``. ``owner_id`` becomes the JWT ``sub``
    (and thus the owner recorded on the resulting upload); ``kind`` selects both
    the target endpoint and the single scope granted."""

    kind: Literal["image", "video"] = Field(examples=["image"])
    owner_id: str = Field(min_length=1, max_length=128, examples=["tenant-42"])
    expires_in_seconds: int = Field(default=300, ge=1, le=86400, examples=[300])


class PresignedUploadResponse(BaseModel):
    url: str = Field(description="Ready-to-use upload URL with the token embedded")
    token: str = Field(description="The signed capability JWT (also embedded in url)")
    expires_at: int = Field(description="Unix timestamp (seconds) when the token expires")
    scope: str = Field(description="The single scope the token grants")


@router.post(
    "/upload/presign",
    tags=["Uploads"],
    summary="Mint a presigned upload URL",
)
def create_presigned_upload(
    body: PresignedUploadRequest,
    _owner: Annotated[str, Depends(verify_static_token)],
) -> PresignedUploadResponse:
    """Issue a short-lived, single-scope capability JWT -- and the ready-to-use
    URL embedding it -- for a direct *browser -> this service* upload, so the
    main backend never has to proxy the bytes.

    **Static master token only** (this mints credentials; a capability JWT may
    not call it). The main backend asks for one of these and hands the returned
    ``url`` to the frontend, which POSTs the file to it with no auth header of
    its own (the token rides in the query string). The backend could sign the
    same JWT itself since the secret is shared -- this is the convenience path.

    Requires ``JWT_SECRET_KEY`` to be configured; 503 otherwise.
    """
    if not settings.JWT_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Presigned uploads are disabled (JWT_SECRET_KEY is not set)",
        )
    scope = SCOPE_UPLOAD_IMAGE if body.kind == "image" else SCOPE_UPLOAD_VIDEO
    issued_at = int(time.time())
    expires_at = issued_at + body.expires_in_seconds
    token = jwt.encode(
        {"sub": body.owner_id, "scopes": [scope], "iat": issued_at, "exp": expires_at},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    path = f"/upload/{body.kind}"
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    url = f"{base}{path}?token={token}" if base else f"{path}?token={token}"
    return PresignedUploadResponse(url=url, token=token, expires_at=expires_at, scope=scope)
