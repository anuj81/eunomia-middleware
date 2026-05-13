"""Auth provider tests — both mock and keycloak (offline, using a synthetic JWKS).

The keycloak path is tested by minting our own RSA key, hand-signing tokens
with python-jose, and seeding the JWKS cache with the matching public key —
so the tests don't need a live Keycloak.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt
from jose.utils import long_to_base64

from src.api import auth as auth_module
from src.config import load_settings, reset_settings_cache


# --------------------------------------------------------------------------- #
# RSA key factory + JWKS shim                                                 #
# --------------------------------------------------------------------------- #


def _rsa_keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048,
                                    backend=default_backend())
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_numbers = priv.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-kid",
        "alg": "RS256",
        "use": "sig",
        "n": long_to_base64(pub_numbers.n).decode("ascii"),
        "e": long_to_base64(pub_numbers.e).decode("ascii"),
    }
    return pem, jwk


def _mint_token(priv_pem: str, *, sub: str, roles: List[str],
                iss: str, exp_in: int = 300,
                aud: Any = None, email: str = "user@example.org",
                username: str = "user") -> str:
    now = int(time.time())
    payload: Dict[str, Any] = {
        "iss": iss,
        "sub": sub,
        "email": email,
        "preferred_username": username,
        "iat": now,
        "exp": now + exp_in,
        "realm_access": {"roles": roles},
    }
    if aud is not None:
        payload["aud"] = aud
    return jwt.encode(payload, priv_pem, algorithm="RS256",
                      headers={"kid": "test-kid"})


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def keycloak_settings(monkeypatch):
    monkeypatch.setenv("EUNOMIA_AUTH__PROVIDER", "keycloak")
    reset_settings_cache()
    auth_module.reset_jwks_cache()
    return load_settings()


@pytest.fixture
def seeded_jwks(monkeypatch):
    """Mint a keypair, seed the auth module's JWKS cache with the public key."""
    priv, pub_jwk = _rsa_keypair()
    auth_module._jwks_cache._jwks = {"keys": [pub_jwk]}
    auth_module._jwks_cache._fetched_at = time.time()
    auth_module._jwks_cache._jwks_url = "test://jwks"
    yield priv
    auth_module.reset_jwks_cache()


class _Req:
    def __init__(self, headers: Dict[str, str]):
        self.headers = headers


# --------------------------------------------------------------------------- #
# mock provider                                                               #
# --------------------------------------------------------------------------- #


def test_mock_provider_known_token():
    auth_module._jwks_cache.invalidate()
    reset_settings_cache()
    # default config has provider=mock
    identity = auth_module.verify_token(_Req({"Authorization": "Bearer finance-token"}))
    assert identity["preferred_username"] == "finance.alice"
    assert "eunomia-finance-user" in identity["roles"]
    assert "eunomia-pii-unmask" in identity["roles"]
    assert identity["provider"] == "mock"


def test_mock_provider_unknown_token_rejected():
    reset_settings_cache()
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_token(_Req({"Authorization": "Bearer not-a-token"}))
    assert exc.value.status_code == 401


def test_mock_provider_om_admin_token_carries_admin_role():
    reset_settings_cache()
    identity = auth_module.verify_token(_Req({"Authorization": "Bearer om-admin-token"}))
    assert "eunomia-om-admin" in identity["roles"]


# --------------------------------------------------------------------------- #
# common: missing / malformed headers                                         #
# --------------------------------------------------------------------------- #


def test_missing_authorization_header():
    reset_settings_cache()
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_token(_Req({}))
    assert exc.value.status_code == 401


def test_authorization_without_bearer_prefix():
    reset_settings_cache()
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_token(_Req({"Authorization": "Token foobar"}))
    assert exc.value.status_code == 401


def test_empty_bearer_token():
    reset_settings_cache()
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_token(_Req({"Authorization": "Bearer "}))
    assert exc.value.status_code == 401


# --------------------------------------------------------------------------- #
# keycloak provider — happy path                                              #
# --------------------------------------------------------------------------- #


def test_keycloak_provider_validates_signed_jwt(keycloak_settings, seeded_jwks):
    token = _mint_token(seeded_jwks,
                         sub="user-1",
                         roles=["eunomia-finance-user", "eunomia-pii-unmask"],
                         iss=keycloak_settings.auth.keycloak.issuer)
    identity = auth_module.verify_token(_Req({"Authorization": f"Bearer {token}"}))
    assert identity["sub"] == "user-1"
    assert identity["roles"] == ["eunomia-finance-user", "eunomia-pii-unmask"]
    assert identity["provider"] == "keycloak"
    assert identity["raw_token"] == token


def test_keycloak_provider_admin_role_present(keycloak_settings, seeded_jwks):
    token = _mint_token(seeded_jwks,
                         sub="om.admin", roles=["eunomia-om-admin"],
                         iss=keycloak_settings.auth.keycloak.issuer)
    identity = auth_module.verify_token(_Req({"Authorization": f"Bearer {token}"}))
    assert "eunomia-om-admin" in identity["roles"]


# --------------------------------------------------------------------------- #
# keycloak provider — failure modes                                           #
# --------------------------------------------------------------------------- #


def test_keycloak_rejects_expired_token(keycloak_settings, seeded_jwks):
    token = _mint_token(seeded_jwks,
                         sub="u", roles=["eunomia-finance-user"],
                         iss=keycloak_settings.auth.keycloak.issuer,
                         exp_in=-60)  # already expired
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_token(_Req({"Authorization": f"Bearer {token}"}))
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_keycloak_rejects_wrong_issuer(keycloak_settings, seeded_jwks):
    token = _mint_token(seeded_jwks,
                         sub="u", roles=["eunomia-finance-user"],
                         iss="http://attacker.example/realms/eunomia")
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_token(_Req({"Authorization": f"Bearer {token}"}))
    assert exc.value.status_code == 401


def test_keycloak_rejects_token_signed_by_wrong_key(keycloak_settings, seeded_jwks):
    # Use a DIFFERENT private key to sign — public key in JWKS cache won't match.
    other_priv, _ = _rsa_keypair()
    token = _mint_token(other_priv,
                         sub="u", roles=["eunomia-finance-user"],
                         iss=keycloak_settings.auth.keycloak.issuer)
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_token(_Req({"Authorization": f"Bearer {token}"}))
    assert exc.value.status_code == 401


def test_keycloak_rejects_tampered_token(keycloak_settings, seeded_jwks):
    token = _mint_token(seeded_jwks,
                         sub="u", roles=["eunomia-finance-user"],
                         iss=keycloak_settings.auth.keycloak.issuer)
    # Tamper: flip one character of the signature
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}.{'X' * 10}{sig[10:]}"
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_token(_Req({"Authorization": f"Bearer {tampered}"}))
    assert exc.value.status_code == 401
