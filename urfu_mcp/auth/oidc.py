"""Configurable native-client OAuth 2.0 Authorization Code + OIDC PKCE flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import httpx
from authlib.oidc.core import CodeIDToken  # type: ignore[import-untyped]
from joserfc import jwk, jwt
from joserfc.errors import JoseError

_ALLOWED_ID_TOKEN_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


class OidcError(Exception):
    """A safe, non-secret-bearing OIDC configuration or protocol failure."""


@dataclass(frozen=True, slots=True)
class OidcConfig:
    """Public-client configuration; no client secret is accepted or persisted."""

    issuer: str
    client_id: str
    redirect_uri: str
    person_id_claim: str = "person_id"
    callback_timeout_seconds: int = 300
    metadata_timeout_seconds: float = 10.0
    metadata_max_bytes: int = 1_000_000
    transaction_ttl_seconds: int = 600

    def __post_init__(self) -> None:
        issuer = urlsplit(self.issuer)
        if (
            issuer.scheme != "https"
            or not issuer.hostname
            or issuer.username is not None
            or issuer.password is not None
            or issuer.query
            or issuer.fragment
        ):
            raise ValueError("issuer must be an HTTPS URL without user info or query")
        if not self.client_id.strip():
            raise ValueError("client_id must not be empty")
        if not self.person_id_claim.strip():
            raise ValueError("person_id_claim must not be empty")
        if self.callback_timeout_seconds <= 0:
            raise ValueError("callback timeout must be positive")
        if self.metadata_timeout_seconds <= 0 or self.metadata_timeout_seconds > 60:
            raise ValueError("metadata timeout must be between 0 and 60 seconds")
        if self.metadata_max_bytes < 1024:
            raise ValueError("metadata size limit must be at least 1024 bytes")
        if self.transaction_ttl_seconds <= 0:
            raise ValueError("transaction lifetime must be positive")

        redirect = urlsplit(self.redirect_uri)
        if (
            not redirect.hostname
            or redirect.username is not None
            or redirect.password is not None
            or redirect.query
            or redirect.fragment
        ):
            raise ValueError("redirect_uri must be an HTTPS or loopback HTTP URL")
        if redirect.scheme == "https":
            return
        try:
            address = ipaddress.ip_address(redirect.hostname)
            loopback = address.is_loopback
        except ValueError:
            loopback = redirect.hostname.lower() == "localhost"
        if redirect.scheme != "http" or not loopback:
            raise ValueError("redirect_uri must be an HTTPS or loopback HTTP URL")


@dataclass(slots=True)
class OidcTransaction:
    """Single-use, short-lived state held by the caller during browser login."""

    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    expires_at: float
    used: bool = False


@dataclass(frozen=True, slots=True)
class OidcLogin:
    authorization_url: str = field(repr=False)
    transaction: OidcTransaction = field(repr=False)


@dataclass(frozen=True, slots=True)
class OidcTokens:
    """Validated tokens returned distinctly; repr never includes token material."""

    access_token: str | None = field(repr=False)
    id_token: str = field(repr=False)
    token_type: str
    expires_at: float | None
    refresh_token: str | None = field(default=None, repr=False)
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False)
    person_id: str | None = field(default=None, repr=False)


class OidcClient:
    """Discover a provider, start PKCE login, and validate the resulting ID token.

    This generic client does not assume that a particular university issuer grants
    a public client, issues refresh tokens, or accepts either token at Modeus APIs.
    The caller must supply an issuer, registered client ID, and registered redirect.
    """

    def __init__(
        self,
        config: OidcConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
        max_metadata_bytes: int | None = None,
        transaction_ttl: int | None = None,
    ) -> None:
        timeout = config.metadata_timeout_seconds if timeout is None else timeout
        max_metadata_bytes = (
            config.metadata_max_bytes if max_metadata_bytes is None else max_metadata_bytes
        )
        transaction_ttl = (
            config.transaction_ttl_seconds if transaction_ttl is None else transaction_ttl
        )
        if timeout <= 0 or max_metadata_bytes <= 0 or transaction_ttl <= 0:
            raise ValueError("timeout, metadata size, and transaction lifetime must be positive")
        self._config = config
        self._transport = transport
        self._timeout = timeout
        self._max_metadata_bytes = max_metadata_bytes
        self._transaction_ttl = transaction_ttl

    async def begin_login(self) -> OidcLogin:
        metadata = await self._discover()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(32)
        transaction = OidcTransaction(
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            expires_at=time.time() + self._transaction_ttl,
        )
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        query = urlencode({
            'response_type': 'code',
            'client_id': self._config.client_id,
            'redirect_uri': self._config.redirect_uri,
            'scope': 'openid profile email',
            'state': state,
            'nonce': nonce,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })
        url = f"{metadata['authorization_endpoint']}?{query}"
        return OidcLogin(authorization_url=url, transaction=transaction)

    async def complete_login(
        self,
        callback_url: str,
        transaction: OidcTransaction,
    ) -> OidcTokens:
        self._consume_transaction(transaction)
        callback = urlsplit(callback_url)
        expected = urlsplit(self._config.redirect_uri)
        if (
            callback.scheme != expected.scheme
            or callback.hostname != expected.hostname
            or callback.port != expected.port
            or callback.path != expected.path
            or callback.fragment
        ):
            raise OidcError("Authorization callback does not match the registered redirect")
        params = parse_qs(callback.query, keep_blank_values=True, strict_parsing=False)
        if "error" in params:
            raise OidcError("The identity provider rejected authorization")
        state_values = params.get("state", [])
        if len(state_values) != 1 or not hmac.compare_digest(state_values[0], transaction.state):
            raise OidcError("Authorization callback state mismatch")
        code_values = params.get("code", [])
        if len(code_values) != 1 or not code_values[0]:
            raise OidcError("Authorization callback has no code")

        metadata = await self._discover()
        token = await self._request_json(
            "POST",
            metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code_values[0],
                "redirect_uri": self._config.redirect_uri,
                "client_id": self._config.client_id,
                "code_verifier": transaction.code_verifier,
            },
        )

        access_token = token.get("access_token")
        id_token = token.get("id_token")
        token_type = token.get("token_type")
        if not isinstance(access_token, str) or not access_token:
            raise OidcError("Token response has no access token")
        if not isinstance(id_token, str) or not id_token:
            raise OidcError("Token response has no ID token")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise OidcError("Token response has an unsupported token type")

        claims = await self._validate_id_token(
            id_token,
            metadata,
            nonce=transaction.nonce,
            access_token=access_token,
        )
        raw_person_id = claims.get(self._config.person_id_claim)
        if not isinstance(raw_person_id, str):
            raise OidcError("ID token does not contain a valid person identity")
        try:
            parsed_person_id = UUID(raw_person_id)
        except ValueError:
            raise OidcError("ID token does not contain a valid person identity") from None
        if parsed_person_id.int == 0:
            raise OidcError("ID token does not contain a valid person identity")
        expires_in = token.get("expires_in")
        expires_at = None
        if (
            isinstance(expires_in, (int, float))
            and not isinstance(expires_in, bool)
            and math.isfinite(float(expires_in))
            and expires_in > 0
        ):
            expires_at = time.time() + float(expires_in)
        refresh_token = token.get("refresh_token")
        return OidcTokens(
            access_token=access_token,
            id_token=id_token,
            token_type=token_type,
            expires_at=expires_at,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            claims=dict(claims),
            person_id=str(parsed_person_id),
        )

    def _consume_transaction(self, transaction: OidcTransaction) -> None:
        if transaction.used:
            raise OidcError("Authorization transaction was already used")
        transaction.used = True
        if transaction.expires_at <= time.time():
            raise OidcError("Authorization transaction expired")

    async def _discover(self) -> dict[str, Any]:
        issuer = self._config.issuer.rstrip("/")
        discovery_url = f"{issuer}/.well-known/openid-configuration"
        document = await self._get_json(discovery_url)
        if document.get("issuer") != self._config.issuer:
            raise OidcError("Provider discovery issuer does not match configuration")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = document.get(key)
            if not isinstance(endpoint, str) or not self._is_https_url(endpoint):
                raise OidcError("Provider discovery has an invalid endpoint")
        response_types = document.get("response_types_supported")
        if not isinstance(response_types, list) or "code" not in response_types:
            raise OidcError("Provider does not advertise authorization code flow")
        challenge_methods = document.get("code_challenge_methods_supported")
        if not isinstance(challenge_methods, list) or "S256" not in challenge_methods:
            raise OidcError("Provider does not advertise S256 PKCE")
        auth_methods = document.get("token_endpoint_auth_methods_supported")
        if not isinstance(auth_methods, list) or "none" not in auth_methods:
            raise OidcError("Provider does not advertise public-client token exchange")
        algorithms = document.get("id_token_signing_alg_values_supported")
        if not isinstance(algorithms, list) or not any(
            algorithm in _ALLOWED_ID_TOKEN_ALGORITHMS for algorithm in algorithms
        ):
            raise OidcError("Provider has no supported asymmetric ID-token algorithm")
        return document

    async def _validate_id_token(
        self,
        id_token: str,
        metadata: Mapping[str, Any],
        *,
        nonce: str,
        access_token: str,
    ) -> Mapping[str, Any]:
        jwks = await self._get_json(metadata["jwks_uri"])
        try:
            key_set = jwk.KeySet.import_key_set(cast(Any, jwks))
            algorithms = [
                algorithm
                for algorithm in metadata["id_token_signing_alg_values_supported"]
                if algorithm in _ALLOWED_ID_TOKEN_ALGORITHMS
            ]
            decoded = jwt.decode(id_token, key_set, algorithms=algorithms)
            options = {
                "iss": {"values": [self._config.issuer]},
                "aud": {"values": [self._config.client_id]},
            }
            oidc_claims = CodeIDToken(
                decoded.claims,
                decoded.header,
                options=options,
                params={
                    "client_id": self._config.client_id,
                    "nonce": nonce,
                    "access_token": access_token,
                },
            )
            oidc_claims.validate(leeway=60)
            aud = oidc_claims.get("aud")
            audiences = [aud] if isinstance(aud, str) else aud
            if (
                not isinstance(audiences, list)
                or self._config.client_id not in audiences
                or any(not isinstance(value, str) for value in audiences)
                or len(set(audiences)) != 1
            ):
                raise ValueError("audience mismatch")
            subject = oidc_claims.get("sub")
            if not isinstance(subject, str) or not subject:
                raise ValueError("subject missing")
            return dict(oidc_claims)
        except (JoseError, ValueError, TypeError, KeyError):
            raise OidcError("ID token validation failed") from None

    async def _get_json(self, url: str) -> dict[str, Any]:
        return await self._request_json("GET", url)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client, client.stream(method, url, data=data) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self._max_metadata_bytes:
                        raise OidcError("Provider response exceeded the size limit")
                    body.extend(chunk)
            result = json.loads(body)
        except OidcError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise OidcError("Provider metadata request failed") from None
        if not isinstance(result, dict):
            raise OidcError("Provider metadata must be a JSON object")
        return result

    @staticmethod
    def _is_https_url(value: str) -> bool:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
