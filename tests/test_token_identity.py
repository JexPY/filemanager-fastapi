"""Per-token identity: config parsing + verify_token returning an owner."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.config import Settings, _derive_owner, settings
from app.routers.auth import verify_token


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _req(query: str = "") -> Request:
    """Minimal ASGI request; `query` (e.g. "token=abc") drives the ?token= path."""
    return Request({"type": "http", "query_string": query.encode(), "headers": []})


def test_bare_token_owner_is_derived_hash_not_the_secret() -> None:
    s = Settings(FILE_MANAGER_BEARER_TOKENS="secret-abc")
    owner = s.token_identities["secret-abc"]
    assert owner == _derive_owner("secret-abc")
    assert owner.startswith("tok_")
    assert owner != "secret-abc"  # never expose the secret as the identity


def test_labeled_token_owner_is_the_label() -> None:
    s = Settings(FILE_MANAGER_BEARER_TOKENS="mobile:secret-abc")
    assert s.token_identities == {"secret-abc": "mobile"}


def test_mixed_labeled_bare_whitespace_and_empty_entries() -> None:
    s = Settings(FILE_MANAGER_BEARER_TOKENS=" mobile:s1 , s2 ,, ")
    assert s.token_identities == {"s1": "mobile", "s2": _derive_owner("s2")}
    assert s.valid_tokens == ["s1", "s2"]


def test_distinct_secrets_get_distinct_owners() -> None:
    s = Settings(FILE_MANAGER_BEARER_TOKENS="a,b")
    owners = set(s.token_identities.values())
    assert len(owners) == 2


def test_verify_token_returns_the_matched_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "alice:tok-a,bob:tok-b")
    assert verify_token(_req(), _creds("tok-a")) == "alice"
    assert verify_token(_req(), _creds("tok-b")) == "bob"


def test_verify_token_accepts_static_token_via_query_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No Authorization header -> the ?token= fallback resolves the same owner.
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "alice:tok-a")
    assert verify_token(_req("token=tok-a"), None) == "alice"


def test_verify_token_rejects_wrong_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FILE_MANAGER_BEARER_TOKENS", "alice:tok-a")
    req = _req()
    creds = _creds("not-a-token")
    with pytest.raises(HTTPException) as wrong:
        verify_token(req, creds)
    assert wrong.value.status_code == 401

    req_empty = _req()
    with pytest.raises(HTTPException) as missing:
        verify_token(req_empty, None)
    assert missing.value.status_code == 401
