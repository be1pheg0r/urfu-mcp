"""Verified iStudent JWT claims from the fixed official Keycloak realm."""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from joserfc import jwk, jwt
from joserfc.errors import JoseError

ISSUER = "https://keys.urfu.ru/auth/realms/urfu-lk"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
MAX_JWKS_BYTES = 1_000_000


class IStudentTokenError(ValueError):
    """Safe failure validating the dedicated iStudent token."""


def validate_istudent_token(token: str, *, now: float, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Check RS256 signature, exact issuer, iStudent authorized party and expiry."""
    if not isinstance(token, str) or len(token) > 65_536 or token.count(".") != 2:
        raise IStudentTokenError("iStudent token is malformed")
    try:
        with httpx.Client(transport=transport, timeout=15, follow_redirects=False) as client:
            response = client.get(JWKS_URL)
            response.raise_for_status()
            if len(response.content) > MAX_JWKS_BYTES:
                raise ValueError
            document = response.json()
        keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(keys, list) or not keys or any(not isinstance(key, dict) for key in keys):
            raise ValueError
        if any({"d", "p", "q", "dp", "dq", "qi", "oth"}.intersection(key) for key in keys):
            raise ValueError
        key_set = jwk.KeySet.import_key_set(cast(Any, {"keys": keys}))
        decoded = jwt.decode(token, key_set, algorithms=["RS256"])
        claims = decoded.claims
        if not isinstance(claims, dict):
            raise TypeError
        if claims.get("iss") != ISSUER or claims.get("azp") != "istudent":
            raise ValueError
        exp, issued = claims.get("exp"), claims.get("iat")
        if type(exp) not in (int, float) or type(issued) not in (int, float):
            raise ValueError
        if cast(float, exp) <= now or cast(float, issued) > now + 60:
            raise ValueError
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError
        return dict(claims)
    except (httpx.HTTPError, JoseError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise IStudentTokenError("iStudent token signature or required claims are invalid") from None


def is_exact_istudent_url(value: str) -> bool:
    """Accept only the fixed iStudent HTTPS host and the BRS overview path."""
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https" and parsed.hostname == "istudent.urfu.ru"
            and parsed.port in (None, 443) and parsed.username is None
            and parsed.password is None and parsed.path == "/s/http-urfu-ru-ru-students-study-brs"
            and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False
