"""Inbound request authentication.

Two providers, selected by `auth.provider` in settings:

    'mock'      — string-match tokens (preserves pre-Phase-D dev flow).
    'keycloak'  — real OIDC JWT validation against the realm's JWKS.

`verify_token` is the FastAPI ``Depends`` target. It returns a UserIdentity
mapping with at minimum:
    {
        "sub":                 str,           # opaque user id
        "email":               str | None,
        "preferred_username":  str | None,
        "roles":               list[str],     # realm_access.roles or mock-derived
        "raw_token":           str,           # forwarded to OM /search/query
        "provider":            "mock"|"keycloak",
    }

The `raw_token` is the bearer the middleware will forward to OpenMetadata.
For mock mode, it's the literal string the caller sent (which works because
OM is also in basic-or-mock auth during offline testing).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, Request
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWKError, JWTError

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Mock provider (Phase A-C parity)                                            #
# --------------------------------------------------------------------------- #

# Mapped to the new Phase D role names so downstream code stops switching
# on the legacy strings. The token names are kept for backward compat with
# existing tests + curl examples.
_MOCK_TOKENS: Dict[str, Dict[str, Any]] = {
    "finance-token":          {"sub": "finance.alice",   "email": "finance.alice@open-metadata.org",
                                "preferred_username": "finance.alice",
                                "roles": ["eunomia-finance-user", "eunomia-pii-unmask"]},
    "external-auditor-token": {"sub": "auditor.bob",     "email": "auditor.bob@open-metadata.org",
                                "preferred_username": "auditor.bob",
                                "roles": ["eunomia-external-auditor"]},
    "marketing-token":        {"sub": "marketing.carol", "email": "marketing.carol@open-metadata.org",
                                "preferred_username": "marketing.carol",
                                "roles": ["eunomia-marketing-lead", "eunomia-pii-unmask"]},
    "agency-token":           {"sub": "agency.dave",     "email": "agency.dave@open-metadata.org",
                                "preferred_username": "agency.dave",
                                "roles": ["eunomia-agency-partner"]},
    "om-admin-token":         {"sub": "om.admin",        "email": "om.admin@open-metadata.org",
                                "preferred_username": "om.admin",
                                "roles": ["eunomia-om-admin", "eunomia-pii-unmask"]},
}


def _verify_mock(token: str) -> Dict[str, Any]:
    identity = _MOCK_TOKENS.get(token)
    if not identity:
        raise HTTPException(status_code=401, detail="Unknown mock token")
    return {**identity, "raw_token": token, "provider": "mock"}


# --------------------------------------------------------------------------- #
# Keycloak provider — JWT validation via JWKS                                 #
# --------------------------------------------------------------------------- #


class _JwksCache:
    """Thread-safe JWKS cache with TTL.

    We keep both the parsed JWKS dict (passed to jose.jwt.decode) and the
    fetch timestamp. Stale cache triggers a re-fetch on next access.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jwks: Optional[Dict[str, Any]] = None
        self._fetched_at: float = 0.0
        self._jwks_url: Optional[str] = None

    def get(self, issuer: str, ttl_seconds: int) -> Dict[str, Any]:
        # Discover the JWKS URL once (then cache it alongside the keys).
        with self._lock:
            now = time.time()
            url = self._jwks_url
            cached = self._jwks
            fetched = self._fetched_at

        if url is None:
            url = self._discover_jwks_url(issuer)
            with self._lock:
                self._jwks_url = url

        if cached is not None and (now - fetched) < ttl_seconds:
            return cached

        # Fetch fresh JWKS.
        try:
            resp = httpx.get(url, timeout=5.0)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            # If we have a stale cache, prefer it over a hard failure.
            if cached is not None:
                logger.warning("JWKS refresh failed (%s); using stale cache.", e)
                return cached
            raise HTTPException(status_code=503, detail=f"JWKS unreachable: {e}")

        jwks = resp.json()
        with self._lock:
            self._jwks = jwks
            self._fetched_at = time.time()
        logger.info(
            "JWKS refreshed from %s (%d keys)", url, len(jwks.get("keys") or [])
        )
        return jwks

    def _discover_jwks_url(self, issuer: str) -> str:
        """Use OIDC discovery to find the JWKS endpoint."""
        well_known = issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            resp = httpx.get(well_known, timeout=5.0)
            resp.raise_for_status()
            return resp.json()["jwks_uri"]
        except Exception as e:
            # Fall back to Keycloak convention if discovery is unreachable.
            fallback = issuer.rstrip("/") + "/protocol/openid-connect/certs"
            logger.warning(
                "OIDC discovery failed for %s (%s); falling back to %s",
                well_known, e, fallback,
            )
            return fallback

    def invalidate(self) -> None:
        """Tests + token rotation use this to force a re-fetch."""
        with self._lock:
            self._jwks = None
            self._fetched_at = 0.0


_jwks_cache = _JwksCache()


def _verify_keycloak(token: str, settings: Settings) -> Dict[str, Any]:
    cfg = settings.auth.keycloak
    jwks = _jwks_cache.get(cfg.issuer, cfg.jwks_cache_seconds)

    options: Dict[str, Any] = {}
    if not cfg.audience:
        # Skip aud check — Keycloak default tokens have empty 'aud'.
        options["verify_aud"] = False

    try:
        claims = jwt.decode(
            token,
            jwks,
            algorithms=[cfg.algorithm],
            issuer=cfg.issuer,
            audience=cfg.audience if cfg.audience else None,
            options=options,
        )
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except JWKError as e:
        # JWKS key id in token doesn't match our cache — refresh once and retry
        # (handles Keycloak key rotation gracefully).
        logger.info("JWK error (%s) — invalidating cache + retrying once.", e)
        _jwks_cache.invalidate()
        jwks = _jwks_cache.get(cfg.issuer, cfg.jwks_cache_seconds)
        try:
            claims = jwt.decode(
                token, jwks,
                algorithms=[cfg.algorithm],
                issuer=cfg.issuer,
                audience=cfg.audience if cfg.audience else None,
                options=options,
            )
        except JWTError as retry_err:
            raise HTTPException(status_code=401, detail=f"Token signature invalid: {retry_err}")
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Token invalid: {e}")

    roles = (claims.get("realm_access") or {}).get("roles") or []
    return {
        "sub":                claims.get("sub"),
        "email":              claims.get("email"),
        "preferred_username": claims.get("preferred_username"),
        "roles":              list(roles),
        "raw_token":          token,
        "provider":           "keycloak",
    }


# --------------------------------------------------------------------------- #
# Public Depends target                                                       #
# --------------------------------------------------------------------------- #


def verify_token(request: Request) -> Dict[str, Any]:
    settings = get_settings()
    auth_header = request.headers.get("Authorization") or ""
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth_header[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty bearer token")

    if settings.auth.provider == "mock":
        return _verify_mock(token)
    if settings.auth.provider == "keycloak":
        return _verify_keycloak(token, settings)
    # Defensive — settings validation should reject other values, but be loud.
    raise HTTPException(
        status_code=500,
        detail=f"Unknown auth provider: {settings.auth.provider!r}",
    )


def reset_jwks_cache() -> None:
    """Test helper. Drops cached JWKS so the next call re-fetches."""
    _jwks_cache.invalidate()
