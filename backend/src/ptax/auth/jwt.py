"""Cognito access-token verification against the pool's JWKS."""

import threading
import time
from typing import Any

import httpx
import jwt

from ptax.config import Settings

JWKS_TTL_SECONDS = 3600


class TokenError(Exception):
    """The bearer token is missing, malformed, expired, or not ours."""


class JWKSCache:
    """Fetches ``{issuer}/.well-known/jwks.json`` and caches keys by ``kid``.

    A ``kid`` miss triggers one refetch (key rotation); a second miss is an error.
    """

    def __init__(self, issuer: str, transport: httpx.BaseTransport | None = None) -> None:
        self._url = f"{issuer.rstrip('/')}/.well-known/jwks.json"
        self._client = httpx.Client(transport=transport, timeout=5.0)
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        response = self._client.get(self._url)
        response.raise_for_status()
        keys = {}
        for jwk in response.json().get("keys", []):
            if "kid" in jwk:
                keys[jwk["kid"]] = jwt.PyJWK(jwk)
        self._keys = keys
        self._fetched_at = time.monotonic()

    def get_key(self, kid: str) -> jwt.PyJWK:
        with self._lock:
            stale = time.monotonic() - self._fetched_at > JWKS_TTL_SECONDS
            if kid not in self._keys or stale:
                self._refresh()
            try:
                return self._keys[kid]
            except KeyError as exc:
                raise TokenError("unknown signing key") from exc


def verify_access_token(token: str, settings: Settings, jwks: JWKSCache) -> dict[str, Any]:
    """Return the claims of a valid Cognito *access* token for our app client."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise TokenError("malformed token") from exc
    if not kid:
        raise TokenError("token has no kid")

    key = jwks.get_key(kid)
    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=settings.cognito_issuer,
            options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    if claims.get("token_use") != "access":
        raise TokenError("not an access token")
    if claims.get("client_id") != settings.cognito_client_id:
        raise TokenError("token issued for a different client")
    return claims
