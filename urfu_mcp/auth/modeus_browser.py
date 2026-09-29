"""Read Modeus OIDC tokens from its authenticated browser session."""

from __future__ import annotations

import base64
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import UUID

import httpx
from authlib.oidc.core import CodeIDToken  # type: ignore[import-untyped]
from joserfc import jwk, jwt
from joserfc.errors import JoseError

from urfu_mcp.auth.credential_store import (
    CredentialRecord,
    CredentialStoreError,
    create_credential_store,
)
from urfu_mcp.auth.oidc import _ALLOWED_ID_TOKEN_ALGORITHMS, OidcTokens
from urfu_mcp.auth.token_store import create_token_store
from urfu_mcp.config import update_auth_settings
from urfu_mcp.elearn.session_auth import looks_like_protected_my_courses
from urfu_mcp.elearn.session_store import (
    ELearnSessionRecord,
    ELearnSessionStoreError,
    create_elearn_session_store,
)
from urfu_mcp.istudent.session_auth import (
    _looks_like_protected_brs as _session_brs_check,
)
from urfu_mcp.istudent.session_auth import (
    validate_istudent_token,
)
from urfu_mcp.istudent.session_store import (
    IStudentSessionRecord,
    IStudentSessionStoreError,
    create_istudent_session_store,
)

TRUSTED_OIDC_ISSUER = "https://sso.urfu.ru/adfs"
_TRUSTED_MODEUS_AUTHORITY = "https://urfu-auth.modeus.org/oauth2/authorize"
_TRUSTED_MODEUS_TOKEN_ISSUER = "https://urfu-auth.modeus.org/oauth2/token"
_TRUSTED_METADATA_HOSTS = frozenset({"sso.urfu.ru", "urfu-auth.modeus.org"})
_MODEUS_URL = "https://urfu.modeus.org/"
_MODEUS_APP_CONFIG_URL = "https://urfu.modeus.org/assets/app.config.json"
_ELEARN_URL = "https://elearn.urfu.ru/my/courses.php"
_ELEARN_HOST = "elearn.urfu.ru"
# Moodle renders the course list progressively, so a valid first response can
# still lack any course link; retry briefly before treating it as a failure.
_ELEARN_PAGE_ATTEMPTS = 5
_ELEARN_PAGE_RETRY_SECONDS = 2.0
_DISCOVERY_LIMIT = 1_000_000
_AUTH_TIMEOUT = 300


class ModeusAuthenticationError(ValueError):
    """Safe error raised when the authenticated Modeus browser session is invalid."""


@dataclass(frozen=True, slots=True)
class BrowserOidcSession:
    """OIDC values; `issuer` holds the session authority for compatibility."""

    issuer: str
    client_id: str
    access_token: str | None = field(repr=False)
    id_token: str = field(repr=False)
    token_type: str
    expires_at: float
    refresh_token: str | None = field(default=None, repr=False)
    oidc_metadata: dict[str, Any] | None = field(default=None, repr=False)
    istudent_cookies: dict[str, str] | None = field(default=None, repr=False)
    elearn_cookies: dict[str, str] | None = field(default=None, repr=False)

    @property
    def authority(self) -> str:
        """Return the authority value stored in the SPA session key."""
        return self.issuer


def parse_oidc_session_storage(
    storage: Mapping[str, str], trusted_issuer: str = TRUSTED_OIDC_ISSUER
) -> BrowserOidcSession:
    """Extract one OIDC user session and its authority from browser storage."""
    matches = [
        (key.removeprefix("oidc.user:"), value)
        for key, value in storage.items()
        if key.startswith("oidc.user:")
    ]
    if len(matches) != 1:
        message = "Modeus browser session is missing or ambiguous"
        raise ModeusAuthenticationError(message)

    session_key, encoded = matches[0]
    if ":" not in session_key:
        raise ModeusAuthenticationError("Modeus token session has invalid settings")
    authority, client_id = session_key.rsplit(":", 1)
    if len(encoded) > _DISCOVERY_LIMIT:
        raise ModeusAuthenticationError("Modeus token session is too large")
    try:
        data = json.loads(encoded)
    except (TypeError, json.JSONDecodeError):
        raise ModeusAuthenticationError("Modeus token session is malformed") from None
    if not isinstance(data, dict):
        raise ModeusAuthenticationError("Modeus token session is malformed")

    trusted_authorities = {trusted_issuer.rstrip("/"), _TRUSTED_MODEUS_AUTHORITY}
    parsed_authority = urlsplit(authority)
    if (
        parsed_authority.scheme != "https"
        or parsed_authority.hostname not in _TRUSTED_METADATA_HOSTS
        or parsed_authority.username is not None
        or parsed_authority.password is not None
        or parsed_authority.query
        or parsed_authority.fragment
        or authority.rstrip("/") not in trusted_authorities
    ):
        location = _safe_issuer_location(authority)
        token_issuer = _unverified_token_issuer(data.get("id_token"))
        token_issuer_hint = f"; ID-token issuer appears as {token_issuer}" if token_issuer else ""
        raise ModeusAuthenticationError(
            f"Modeus token session has an untrusted authority at {location}"
            f"{token_issuer_hint}"
        )
    if not client_id or any(character.isspace() for character in client_id):
        raise ModeusAuthenticationError("Modeus token session has invalid settings")

    access_token = data.get("access_token")
    id_token = data.get("id_token")
    token_type = data.get("token_type", "Bearer")
    expires_at = data.get("expires_at")
    refresh_token = data.get("refresh_token")
    if access_token == "":
        access_token = None
    if access_token is not None and (
        not isinstance(access_token, str) or not access_token
    ):
        raise ModeusAuthenticationError("Modeus token session has an invalid access token")
    if not isinstance(id_token, str) or not id_token:
        raise ModeusAuthenticationError("Modeus token session is missing an ID token")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise ModeusAuthenticationError("Modeus token session has an unsupported token type")
    if expires_at is None:
        # `expires_at` is client-library storage metadata; the signed ID-token exp is authoritative.
        expires_at = math.inf
    elif (
        not isinstance(expires_at, (int, float))
        or isinstance(expires_at, bool)
        or not math.isfinite(float(expires_at))
    ):
        raise ModeusAuthenticationError("Modeus token session has an invalid expiry")
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise ModeusAuthenticationError("Modeus token session has an invalid refresh token")

    return BrowserOidcSession(
        issuer=authority.rstrip("/"),
        client_id=client_id,
        access_token=access_token,
        id_token=id_token,
        token_type="Bearer",
        expires_at=float(expires_at),
        refresh_token=refresh_token,
    )


def parse_browser_storage(
    local_storage: Mapping[str, Any], session_storage: Mapping[str, Any]
) -> BrowserOidcSession:
    """Find the OIDC session in either browser storage area without exposing values."""
    combined = {
        key: value
        for storage in (local_storage, session_storage)
        for key, value in storage.items()
        if isinstance(value, str)
    }
    return parse_oidc_session_storage(combined)


def modeus_url() -> str:
    """Return the official Modeus entry page used to establish the browser session."""
    return _MODEUS_URL


def fetch_oidc_metadata(issuer: str) -> dict[str, Any]:
    """Fetch OIDC metadata only for the trusted issuer found after Modeus sign-in."""
    parsed_issuer = urlsplit(issuer)
    if (
        parsed_issuer.scheme != "https"
        or parsed_issuer.hostname != "sso.urfu.ru"
        or parsed_issuer.path.rstrip("/") != "/adfs"
        or parsed_issuer.username is not None
        or parsed_issuer.password is not None
        or parsed_issuer.query
        or parsed_issuer.fragment
    ):
        raise ModeusAuthenticationError("Modeus token session has an untrusted issuer")
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        response = httpx.get(discovery_url, timeout=15, follow_redirects=False)
        response.raise_for_status()
        if len(response.content) > _DISCOVERY_LIMIT:
            raise ModeusAuthenticationError("Identity-provider metadata is too large")
        metadata = response.json()
    except ModeusAuthenticationError:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise ModeusAuthenticationError("Could not discover the Modeus sign-in settings") from None
    if (
        not isinstance(metadata, dict)
        or metadata.get("issuer") != issuer.rstrip("/")
        or not _trusted_https_url(metadata.get("jwks_uri"))
    ):
        raise ModeusAuthenticationError("Identity-provider metadata is invalid")
    return metadata


def _parse_captured_oidc_metadata(
    response_url: str, status: int, body: bytes
) -> dict[str, Any] | None:
    """Accept only bounded OIDC discovery JSON from the known URFU/Modeus hosts."""
    source = urlsplit(response_url)
    try:
        source_port = source.port
    except ValueError:
        return None
    if (
        status != 200
        or source.scheme != "https"
        or source_port not in (None, 443)
        or source.hostname not in _TRUSTED_METADATA_HOSTS
        or source.username is not None
        or source.password is not None
        or source.query
        or source.fragment
        or not source.path.endswith("/.well-known/openid-configuration")
        or len(body) > _DISCOVERY_LIMIT
    ):
        return None
    try:
        metadata = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    issuer = metadata.get("issuer")
    algorithms = metadata.get("id_token_signing_alg_values_supported")
    if (
        not isinstance(issuer, str)
        or not _trusted_issuer_url(issuer)
        or not _trusted_https_url(metadata.get("jwks_uri"))
        or not isinstance(algorithms, list)
        or not any(algorithm in _ALLOWED_ID_TOKEN_ALGORITHMS for algorithm in algorithms)
    ):
        return None
    return metadata


def _parse_captured_modeus_app_config(
    response_url: str, status: int, body: bytes
) -> dict[str, Any] | None:
    """Read Modeus's public issuer/client/JWKS config from its exact HTTPS asset."""
    source = urlsplit(response_url)
    try:
        source_port = source.port
    except ValueError:
        return None
    if (
        status != 200
        or source.scheme != "https"
        or source.hostname != "urfu.modeus.org"
        or source_port not in (None, 443)
        or source.username is not None
        or source.password is not None
        or source.path != "/assets/app.config.json"
        or source.query
        or source.fragment
        or len(body) > _DISCOVERY_LIMIT
    ):
        return None
    try:
        document = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("wso"), dict):
        return None
    wso = document["wso"]
    issuer = wso.get("issuer")
    client_id = wso.get("clientId")
    jwks = wso.get("jwks")
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if (
        not _trusted_issuer_url(issuer)
        or not isinstance(client_id, str)
        or not client_id
        or any(character.isspace() for character in client_id)
        or not isinstance(keys, list)
        or not keys
        or any(not isinstance(key, dict) for key in keys)
    ):
        return None
    private_members = {"d", "p", "q", "dp", "dq", "qi", "oth", "k"}
    if any(private_members.intersection(key) for key in keys):
        return None
    algorithms = sorted(
        {
            key.get("alg")
            for key in keys
            if key.get("alg") in _ALLOWED_ID_TOKEN_ALGORITHMS
            and key.get("use", "sig") == "sig"
        }
    )
    if not algorithms:
        return None
    try:
        jwk.KeySet.import_key_set(cast(Any, jwks))
    except (JoseError, TypeError, ValueError):
        return None
    return {
        "issuer": issuer,
        "client_id": client_id,
        "jwks": jwks,
        "id_token_signing_alg_values_supported": algorithms,
        "_metadata_source": _MODEUS_APP_CONFIG_URL,
    }


def _capture_oidc_metadata(response: Any, collected: list[dict[str, Any]]) -> None:
    """Retain only bounded discovery documents from the trusted login hosts."""
    try:
        body = response.body()
        metadata = _parse_captured_oidc_metadata(
            response.url, response.status, body
        ) or _parse_captured_modeus_app_config(response.url, response.status, body)
    except Exception:  # noqa: BLE001 - response details may contain sensitive data
        return
    if metadata is not None and metadata not in collected:
        collected.append(metadata)


def _select_oidc_metadata(
    candidates: list[dict[str, Any]], id_token: str, client_id: str
) -> dict[str, Any]:
    """Match captured metadata to the token issuer and SPA client ID."""
    issuer = _read_unverified_token_issuer(id_token)
    if issuer is None:
        raise ModeusAuthenticationError("ID token has no readable issuer claim")
    if not _trusted_issuer_url(issuer):
        raise ModeusAuthenticationError(
            f"ID token has an untrusted issuer at {_safe_issuer_location(issuer)}"
        )
    if not candidates:
        raise ModeusAuthenticationError(
            "No trusted OIDC discovery document was captured from the sign-in browser"
        )

    matches: list[dict[str, Any]] = []
    for metadata in candidates:
        if (metadata.get("issuer") == issuer
            and metadata.get("client_id", client_id) == client_id
            and metadata not in matches):
            matches.append(metadata)
    if not matches:
        observed_issuers = sorted(
            {
                _safe_issuer_location(value)
                for metadata in candidates
                if isinstance((value := metadata.get("issuer")), str)
            }
        )
        observed = ", ".join(observed_issuers[:4]) or "none"
        if any(metadata.get("issuer") == issuer for metadata in candidates):
            raise ModeusAuthenticationError("Trusted Modeus client ID does not match browser session")
        raise ModeusAuthenticationError(
            "Trusted discovery issuer does not match ID-token issuer "
            f"({_safe_issuer_location(issuer)}; captured: {observed})"
        )
    if len(matches) > 1:
        raise ModeusAuthenticationError(
            "Multiple distinct trusted OIDC discovery documents match the ID token"
        )
    return matches[0]


def validate_oidc_session(
    session: BrowserOidcSession, metadata: Mapping[str, Any]
) -> OidcTokens:
    """Verify a Modeus SPA token signature, issuer, audience, and identity claims."""
    try:
        algorithms = metadata.get("id_token_signing_alg_values_supported")
        jwks_uri = metadata.get("jwks_uri")
        embedded_jwks = metadata.get("jwks")
        uses_embedded_jwks = metadata.get("_metadata_source") == _MODEUS_APP_CONFIG_URL
        if (
            not isinstance(metadata.get("issuer"), str)
            or not _trusted_issuer_url(metadata.get("issuer"))
            or not isinstance(algorithms, list)
            or (uses_embedded_jwks and metadata.get("client_id") != session.client_id)
            or not (
                (uses_embedded_jwks and isinstance(embedded_jwks, dict) and jwks_uri is None)
                or (not uses_embedded_jwks and isinstance(jwks_uri, str)
                    and _trusted_https_url(jwks_uri) and embedded_jwks is None)
            )
        ):
            raise ValueError
        accepted_algorithms = [
            value for value in algorithms if value in _ALLOWED_ID_TOKEN_ALGORITHMS
        ]
        if not accepted_algorithms:
            raise ValueError
        if uses_embedded_jwks:
            key_set = jwk.KeySet.import_key_set(cast(Any, embedded_jwks))
        else:
            assert isinstance(jwks_uri, str)
            jwks_response = httpx.get(jwks_uri, timeout=15, follow_redirects=False)
            jwks_response.raise_for_status()
            if len(jwks_response.content) > _DISCOVERY_LIMIT:
                raise ValueError
            key_set = jwk.KeySet.import_key_set(cast(Any, jwks_response.json()))
        decoded = jwt.decode(session.id_token, key_set, algorithms=accepted_algorithms)
        claims = CodeIDToken(
            decoded.claims,
            decoded.header,
            options={
                "iss": {"values": [metadata["issuer"]]},
                "aud": {"values": [session.client_id]},
            },
            params={"client_id": session.client_id, "access_token": session.access_token},
        )
        claims.validate(leeway=60)
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if (
            not isinstance(audiences, list)
            or audiences != [session.client_id]
            or not isinstance(claims.get("sub"), str)
            or not claims.get("sub")
        ):
            raise ValueError
        raw_person_id = claims.get("person_id")
        if not isinstance(raw_person_id, str):
            raise TypeError
        person_id = UUID(raw_person_id)
        if person_id.int == 0:
            raise ValueError
        expires_at = min(session.expires_at, float(claims["exp"]))
        if expires_at <= time.time():
            raise ValueError
    except (httpx.HTTPError, JoseError, ValueError, TypeError, KeyError):
        raise ModeusAuthenticationError("Modeus authentication token validation failed") from None
    return OidcTokens(
        access_token=session.access_token,
        id_token=session.id_token,
        token_type="Bearer",
        expires_at=expires_at,
        refresh_token=session.refresh_token,
        claims=dict(claims),
        person_id=str(person_id),
    )


def run_modeus_login(config_path: str = "config.yaml") -> bool:
    """Open Modeus sign-in, validate SPA tokens, and save them to the OS keyring."""
    stage = "opening browser session"
    try:
        session = _open_browser_session()
        stage = "validating and storing Modeus tokens"
        _persist_modeus_session(session, config_path)
    except ModeusAuthenticationError as error:
        print(f"Modeus sign-in failed while {stage}: {error}")
        return False
    except Exception as error:  # noqa: BLE001 - exception text may contain secrets
        print(
            f"Modeus sign-in failed while {stage} "
            f"({type(error).__name__}); details were suppressed."
        )
        return False
    print("Modeus sign-in completed; tokens were stored securely.")
    return True


def run_unified_login(
    config_path: str = "config.yaml", *, credential_mode: str = "manual"
) -> bool:
    """Verify both same-context sessions, then persist the independently verified records."""
    stage = "opening browser session"
    try:
        session = _open_browser_session(visit_istudent=True, credential_mode=credential_mode)
        stage = "validating iStudent session"
        tokens = validate_oidc_session(session, session.oidc_metadata or {})
        cookies = session.istudent_cookies
        if cookies is None:
            raise ModeusAuthenticationError("iStudent session cookies are missing")
        claims = validate_istudent_token(cookies["keycloakAccessToken"], now=time.time())
        subject = claims.get("sub")
        expiry = claims.get("exp")
        if not isinstance(subject, str) or not subject or type(expiry) not in (int, float):
            raise ModeusAuthenticationError("iStudent token identity is invalid")
        response = _fetch_protected_istudent_page(cookies)
        if not _looks_like_protected_brs(response.text):
            raise ModeusAuthenticationError("iStudent protected BRS page was not verified")
        stage = "saving verified sessions"
        record = IStudentSessionRecord(
            modeus_person_id=cast(str, tokens.person_id),
            istudent_subject=subject,
            access_token=cookies["keycloakAccessToken"],
            php_session_id=cookies["PHPSESSID"],
            expires_at=min(cast(float, expiry), time.time() + 900),
        )
        create_token_store().save(tokens)
        update_auth_settings(config_path, issuer=str((session.oidc_metadata or {})["issuer"]),
                             client_id=session.client_id, token_kind="id_token")
        create_istudent_session_store().save(record)
    except Exception as error:  # noqa: BLE001 - backend details may contain secrets
        diagnostic = (
            str(error)
            if isinstance(error, ModeusAuthenticationError)
            else "iStudent session identity is invalid"
            if str(error) == "Stored iStudent session identity is invalid"
            else "Could not store the iStudent session securely"
            if isinstance(error, IStudentSessionStoreError)
            else type(error).__name__
        )
        print(f"Unified sign-in incomplete while {stage} ({diagnostic}); details suppressed.")
        return False
    print("Unified sign-in completed; verified sessions were stored securely.")
    return True


def run_elearn_login(
    config_path: str = "config.yaml", *, credential_mode: str = "manual"
) -> bool:
    """Sign in to eLearn separately and store a verified Moodle session.

    eLearn is an independent integration: it must not be able to invalidate an
    already verified Modeus/iStudent sign-in, so it runs as its own command.
    """
    stage = "opening browser session"
    try:
        session = _open_browser_session(
            visit_elearn=True, credential_mode=credential_mode
        )
        stage = "validating eLearn session"
        cookies = session.elearn_cookies
        if cookies is None:
            raise ModeusAuthenticationError("eLearn session cookies are missing")
        response = _fetch_protected_elearn_page(cookies)
        if not _looks_like_protected_elearn(response.text):
            raise ModeusAuthenticationError("eLearn protected course page was not verified")
        stage = "saving verified eLearn session"
        # Moodle exposes no signed subject like iStudent, so the record binds to
        # the locally selected Modeus person and is re-verified on every use.
        person_id = _stored_modeus_person_id()
        record = ELearnSessionRecord(
            modeus_person_id=person_id,
            moodle_session_id=cookies["MoodleSession"],
            sesskey=cookies.get("MDL_SSP_SessID"),
            expires_at=time.time() + 900,
        )
        create_elearn_session_store().save(record)
    except Exception as error:  # noqa: BLE001 - backend details may contain secrets
        diagnostic = (
            str(error)
            if isinstance(error, (ModeusAuthenticationError, ELearnSessionStoreError))
            else type(error).__name__
        )
        print(f"eLearn sign-in incomplete while {stage} ({diagnostic}); details suppressed.")
        return False
    print("eLearn sign-in completed; the verified session was stored securely.")
    return True


def _stored_modeus_person_id() -> str:
    """Read the already authenticated Modeus person that eLearn binds to."""
    try:
        tokens = create_token_store().load()
    except Exception:  # noqa: BLE001 - keyring errors may contain secret details
        raise ModeusAuthenticationError("Could not load the stored Modeus identity") from None
    person_id = getattr(tokens, "person_id", None)
    if not isinstance(person_id, str) or not person_id:
        raise ModeusAuthenticationError("Sign in to Modeus before eLearn")
    return person_id


def _fetch_protected_elearn_page(cookies: Mapping[str, str]) -> httpx.Response:
    """Fetch only the fixed protected eLearn course page after the browser closed.

    Moodle renders the course list progressively, so a valid first response can
    still lack any course link. Retry a bounded number of times before failing.
    """
    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    last_status = 0
    last_content_type = ""
    last_html = ""
    try:
        with httpx.Client(follow_redirects=False, timeout=20) as client:
            for attempt in range(_ELEARN_PAGE_ATTEMPTS):
                response = client.get(_ELEARN_URL, headers={
                    "Cookie": cookie_header,
                    "Accept": "text/html",
                    "User-Agent": "urfu-mcp/0.1",
                })
                last_status = response.status_code
                last_content_type = response.headers.get("content-type", "").lower()
                last_html = response.text
                if (last_status == 200 and last_content_type.startswith("text/html")
                        and _looks_like_protected_elearn(response.text)):
                    return response
                if attempt < _ELEARN_PAGE_ATTEMPTS - 1:
                    time.sleep(_ELEARN_PAGE_RETRY_SECONDS)
    except httpx.HTTPError:
        raise ModeusAuthenticationError("eLearn protected page request failed") from None
    if last_status != 200:
        raise ModeusAuthenticationError("eLearn protected page returned an invalid status")
    if not last_content_type.startswith("text/html"):
        raise ModeusAuthenticationError("eLearn protected page returned an invalid content type")
    # 200 and text/html, but the course list never rendered into the page.
    # Page shape only: never course names, ids, cookies or personal data.
    print(f"eLearn page shape: {_describe_elearn_page_shape(last_html)}")
    raise ModeusAuthenticationError("eLearn protected course page was not verified")


def _looks_like_protected_elearn(html: str) -> bool:
    """Reuse the same structural check as the stored-session provider."""
    return looks_like_protected_my_courses(html)


def _describe_elearn_page_shape(html: str) -> str:
    """Describe page SHAPE only for diagnostics: no names, ids or cookies."""
    from urfu_mcp.html_tree import _HTMLTree

    tree = _HTMLTree()
    try:
        tree.feed(html)
    except ValueError:
        return "unparseable"
    nodes = tree.root.descendants()
    bodies = [node for node in nodes if node.tag == "body"]
    anonymous = any("notloggedin" in node.classes() for node in bodies)
    course_links = 0
    for node in nodes:
        if node.tag == "a" and "course/view.php" in node.attrs.get("href", ""):
            course_links += 1
    has_login_form = any(node.tag == "form" for node in nodes)
    return f"anonymous={anonymous} course_links={course_links} has_form={has_login_form}"


def _fetch_protected_istudent_page(cookies: Mapping[str, str]) -> httpx.Response:
    """Fetch only the fixed protected BRS page after the browser has closed."""
    url = "https://istudent.urfu.ru/s/http-urfu-ru-ru-students-study-brs"
    try:
        with httpx.Client(follow_redirects=False, timeout=20) as client:
            response = client.get(url, headers={
                "Cookie": f"PHPSESSID={cookies['PHPSESSID']}; keycloakAccessToken={cookies['keycloakAccessToken']}",
                "Accept": "text/html",
                "User-Agent": "urfu-mcp/0.1",
            })
    except httpx.HTTPError:
        raise ModeusAuthenticationError("iStudent protected page request failed") from None
    if response.status_code != 200:
        raise ModeusAuthenticationError("iStudent protected page returned an invalid status")
    if not response.headers.get("content-type", "").lower().startswith("text/html"):
        raise ModeusAuthenticationError("iStudent protected page returned an invalid content type")
    return response


def _looks_like_protected_brs(html: str) -> bool:
    return _session_brs_check(html)


def _persist_modeus_session(session: BrowserOidcSession, config_path: str) -> None:
    metadata = session.oidc_metadata
    if metadata is None and session.authority == TRUSTED_OIDC_ISSUER:
        metadata = fetch_oidc_metadata(TRUSTED_OIDC_ISSUER)
    if metadata is None:
        raise ModeusAuthenticationError(
            "No trusted OIDC discovery document was captured during sign-in"
        )
    tokens = validate_oidc_session(session, metadata)
    create_token_store().save(tokens)
    update_auth_settings(
        config_path,
        issuer=str(metadata["issuer"]),
        client_id=session.client_id,
        token_kind="id_token",
    )


def _open_browser_session(
    *, visit_istudent: bool = False, visit_elearn: bool = False,
    credential_mode: str = "manual",
) -> BrowserOidcSession:
    """Use a visible Chromium window so the user can complete SSO and MFA."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ModeusAuthenticationError("Browser support is not installed") from None

    credentials = None
    if credential_mode not in {"manual", "saved"}:
        raise ModeusAuthenticationError("Unsupported sign-in method")
    if credential_mode == "saved":
        try:
            credentials = create_credential_store().load_any()
        except CredentialStoreError:
            raise ModeusAuthenticationError("Could not load credentials from the system keyring") from None
        if credentials is None:
            raise ModeusAuthenticationError("No stashed credentials are available")

    stage = ["playwright_startup"]
    try:
        return _open_browser_session_at_stage(
            sync_playwright, credentials, visit_istudent, visit_elearn,
            PlaywrightTimeoutError, stage
        )
    except Exception as error:  # noqa: BLE001 - suppress browser exception details
        raise ModeusAuthenticationError(
            f"Browser session failed at stage {stage[0]} ({type(error).__name__})"
        ) from None


def _open_browser_session_at_stage(
    sync_playwright: Any, credentials: Any, visit_istudent: bool, visit_elearn: bool,
    PlaywrightTimeoutError: type[Exception], stage: list[str],
) -> BrowserOidcSession:
    with sync_playwright() as playwright:
        browser = _launch_chromium(playwright)
        captured_metadata: list[dict[str, Any]] = []
        try:
            context = browser.new_context()
            try:
                page = context.new_page()
                page.on(
                    "response",
                    lambda response: _capture_oidc_metadata(response, captured_metadata),
                )
                stage[0] = "modeus_page_goto"
                page.goto(_MODEUS_URL, wait_until="load", timeout=60_000)
                if credentials is not None:
                    print("Using saved credentials for URFU SSO; complete MFA if requested.")
                    stage[0] = "modeus_saved_autofill"
                    _wait_and_fill_saved_credentials(page, credentials, PlaywrightTimeoutError)
                else:
                    print("Complete URFU sign-in in the opened browser; waiting for Modeus.")
                try:
                    stage[0] = "modeus_oidc_wait"
                    if credentials is not None:
                        _wait_for_saved_modeus_session(page, PlaywrightTimeoutError)
                    else:
                        page.wait_for_function(
                            "() => { const hasOidc = s => Object.keys(s).some("
                            "key => key.startsWith('oidc.user:')); "
                            "return location.hostname === 'urfu.modeus.org' && "
                            "(hasOidc(localStorage) || hasOidc(sessionStorage)); }",
                            timeout=_AUTH_TIMEOUT * 1000,
                        )
                except PlaywrightTimeoutError:
                    raise ModeusAuthenticationError(
                        "No Modeus OIDC session appeared after sign-in. "
                        "The current sign-in session format may not be supported."
                    ) from None
                stage[0] = "browser_storage_evaluate"
                storage = page.evaluate(
                    "() => ({localStorage: Object.fromEntries(Object.keys(localStorage).map("
                    "key => [key, localStorage.getItem(key)])), "
                    "sessionStorage: Object.fromEntries(Object.keys(sessionStorage).map("
                    "key => [key, sessionStorage.getItem(key)]))})"
                )
                if (
                    not isinstance(storage, dict)
                    or not isinstance(storage.get("localStorage"), dict)
                    or not isinstance(storage.get("sessionStorage"), dict)
                ):
                    raise ModeusAuthenticationError(
                        "Modeus did not return an authenticated browser session"
                    )
                session = parse_browser_storage(storage["localStorage"], storage["sessionStorage"])
                metadata = _select_oidc_metadata(captured_metadata, session.id_token, session.client_id)
                if visit_istudent:
                    # Keep Modeus's SPA page intact while iStudent completes its
                    # own redirects. Both pages share one explicit SSO context.
                    istudent_page = context.new_page()
                    stage[0] = "istudent_page_goto"
                    istudent_page.goto(
                        "https://istudent.urfu.ru/s/http-urfu-ru-ru-students-study-brs",
                        wait_until="load", timeout=60_000,
                    )
                    if credentials is not None and istudent_page.url != (
                        "https://istudent.urfu.ru/s/http-urfu-ru-ru-students-study-brs"
                    ):
                        stage[0] = "istudent_saved_autofill"
                        _wait_and_fill_saved_credentials(
                            istudent_page, credentials, PlaywrightTimeoutError,
                            istudent_flow=True,
                        )
                    try:
                        stage[0] = "istudent_wait_for_url"
                        istudent_page.wait_for_url(
                            "https://istudent.urfu.ru/s/http-urfu-ru-ru-students-study-brs",
                            timeout=_AUTH_TIMEOUT * 1000,
                        )
                    except PlaywrightTimeoutError:
                        raise ModeusAuthenticationError(
                            "iStudent SSO did not complete; manual login or MFA may be required"
                        ) from None
                    stage[0] = "cookie_capture"
                    istudent_cookies = _capture_istudent_cookies(context)
                else:
                    istudent_cookies = None
                if visit_elearn:
                    # eLearn shares the same URFU Keycloak but owns a separate
                    # Moodle session, so it gets its own tab like iStudent.
                    elearn_page = context.new_page()
                    stage[0] = "elearn_page_goto"
                    elearn_page.goto(
                        _ELEARN_URL,
                        wait_until="load", timeout=60_000,
                    )
                    if credentials is not None and not _is_elearn_course_page(elearn_page.url):
                        stage[0] = "elearn_saved_autofill"
                        _wait_and_fill_saved_credentials(
                            elearn_page, credentials, PlaywrightTimeoutError,
                            elearn_flow=True,
                        )
                    try:
                        stage[0] = "elearn_wait_for_url"
                        elearn_page.wait_for_url(_ELEARN_URL, timeout=_AUTH_TIMEOUT * 1000)
                    except PlaywrightTimeoutError:
                        raise ModeusAuthenticationError(
                            "eLearn SSO did not complete; manual login or MFA may be required"
                        ) from None
                    stage[0] = "elearn_cookie_capture"
                    elearn_cookies = _capture_elearn_cookies(context)
                else:
                    elearn_cookies = None
                return replace(
                    session,
                    oidc_metadata=metadata,
                    istudent_cookies=istudent_cookies,
                    elearn_cookies=elearn_cookies,
                )
            finally:
                if sys.exc_info()[0] is None:
                    stage[0] = "context_cleanup"
                context.close()
        finally:
            browser.close()


def _capture_istudent_cookies(context: Any) -> dict[str, str]:
    """Capture only the exact protected iStudent host's required session cookies."""
    cookies = context.cookies("https://istudent.urfu.ru/")
    if not isinstance(cookies, list):
        raise ModeusAuthenticationError("iStudent session cookies are invalid")
    required = {"PHPSESSID", "keycloakAccessToken"}
    selected: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        name = cookie.get("name")
        if name not in required:
            continue
        if (
            cookie.get("domain") != "istudent.urfu.ru"
            or cookie.get("path") != "/"
            or name in selected
            or not isinstance(cookie.get("value"), str)
            or not cookie["value"]
        ):
            raise ModeusAuthenticationError("iStudent session cookies are invalid")
        selected[name] = cookie["value"]
    if set(selected) != required:
        raise ModeusAuthenticationError("iStudent session cookies are missing")
    return selected


def _is_elearn_course_page(value: str) -> bool:
    """Accept only the exact protected eLearn course-list URL."""
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https" and parsed.hostname == _ELEARN_HOST
            and parsed.port in (None, 443) and parsed.username is None
            and parsed.password is None and parsed.path == "/my/courses.php"
            and not parsed.fragment
        )
    except ValueError:
        return False


def _capture_elearn_cookies(context: Any) -> dict[str, str]:
    """Capture only the Moodle session cookies for the exact eLearn host.

    The Moodle SAML plugin also sets MDL_SSP_SessID; it is retained when
    present because the login flow may still need it, but it is not required.
    """
    cookies = context.cookies(f"https://{_ELEARN_HOST}/")
    if not isinstance(cookies, list):
        raise ModeusAuthenticationError("eLearn session cookies are invalid")
    required = "MoodleSession"
    optional = {"MDL_SSP_SessID"}
    selected: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        name = cookie.get("name")
        if name != required and name not in optional:
            continue
        if (
            cookie.get("domain") not in {_ELEARN_HOST, f".{_ELEARN_HOST}"}
            or cookie.get("path") != "/"
            or name in selected
            or not isinstance(cookie.get("value"), str)
            or not cookie["value"]
        ):
            raise ModeusAuthenticationError("eLearn session cookies are invalid")
        selected[name] = cookie["value"]
    if required not in selected:
        raise ModeusAuthenticationError("eLearn session cookies are missing")
    return selected


def _launch_chromium(playwright: Any) -> Any:
    """Install Chromium on first auth use, keeping the documented path two-step."""
    if not Path(playwright.chromium.executable_path).is_file():
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise ModeusAuthenticationError("Could not install the sign-in browser") from None
    try:
        return playwright.chromium.launch(headless=False)
    except Exception:  # noqa: BLE001 - browser errors can expose local system details
        raise ModeusAuthenticationError("Could not start the sign-in browser") from None


def _trusted_adfs_form_action(page: Any, form: Any) -> bool:
    """Allow same-origin ADFS form targets, including normal protocol query strings."""
    try:
        current = urlsplit(page.url)
        action = urlsplit(urljoin(page.url, form.get_attribute("action") or page.url))
        current_port, action_port = current.port, action.port
    except (TypeError, ValueError):
        return False
    return (
        current.scheme == "https" and current.hostname == "sso.urfu.ru"
        and current_port in (None, 443) and current.username is None and current.password is None
        and current.path == "/adfs/ls/" and not current.fragment
        and action.scheme == "https" and action.hostname == current.hostname
        and action_port in (None, 443) and action.username is None and action.password is None
        and not action.fragment
    )


def _trusted_istudent_keycloak_authorization(page: Any) -> bool:
    """Accept only the fixed HTTPS authorization endpoint for the iStudent client."""
    try:
        parsed = urlsplit(page.url)
        port = parsed.port
        clients = parse_qs(parsed.query, keep_blank_values=True).get("client_id", [])
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "keys.urfu.ru"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/auth/realms/urfu-lk/protocol/openid-connect/auth"
        and not parsed.fragment
        and clients == ["istudent"]
    )


def _is_keys_identity_provider_page(page: Any) -> bool:
    try:
        return urlsplit(page.url).hostname == "keys.urfu.ru"
    except (AttributeError, TypeError, ValueError):
        return False


def _trusted_istudent_federation_link(page: Any) -> Any | None:
    """Identify the unique same-tab iStudent Keycloak SAML broker link."""
    if not _trusted_istudent_keycloak_authorization(page):
        return None
    links = page.locator("a")
    if links.count() == 0:
        return None
    matches: list[Any] = []
    for index in range(links.count()):
        link = links.nth(index)
        try:
            raw_href = link.get_attribute("href") or ""
            href = urlsplit(urljoin(page.url, raw_href))
            port = href.port
            # The observed Keycloak broker query is plain form syntax. Reject
            # escaped separators/equality and malformed escapes before decoding,
            # so parse_qs cannot reinterpret data as additional fields.
            if re.search(r"%(?![0-9A-Fa-f]{2})|%(?:26|3[Dd]|3[Ff])", href.query):
                continue
            query = parse_qs(href.query, keep_blank_values=True, strict_parsing=True)
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            href.scheme == "https"
            and href.hostname == "keys.urfu.ru"
            and port in (None, 443)
            and href.username is None
            and href.password is None
            and href.path == "/auth/realms/urfu-lk/broker/saml/login"
            and not href.fragment
            and query.get("client_id") == ["istudent"]
            and all(len(values) == 1 and values[0] for values in query.values())
            and set(query) == {"client_data", "client_id", "session_code", "tab_id"}
            and all(href.query.count(f"{key}=") == 1 for key in query)
            and link.get_attribute("target") in (None, "", "_self")
        ):
            matches.append(link)
    return matches[0] if len(matches) == 1 else None


def _page_location(page: Any) -> str:
    """Return scheme, host and path only; never the query, which may hold tokens."""
    try:
        parts = urlsplit(page.url)
    except (AttributeError, TypeError, ValueError):
        return "unknown"
    return f"{parts.scheme}://{parts.hostname}{parts.path}"


def _unique_keycloak_saml_broker_link(page: Any) -> Any:
    """Return the single verified Keycloak federation link for the eLearn SAML flow.

    eLearn authenticates through Keycloak's SAML endpoint, whose broker link
    carries an eLearn client id that is not asserted here. The link is still
    constrained to the exact realm broker path on the exact HTTPS host, so it
    can only lead to URFU's own federation broker, and only the resulting
    official ADFS form is ever filled.
    """
    if not _is_keys_identity_provider_page(page):
        return None
    try:
        links = page.query_selector_all("a[href]")
    except Exception:  # noqa: BLE001 - browser errors can expose local system details
        return None
    allowed = {"client_data", "client_id", "session_code", "tab_id"}
    matches: list[Any] = []
    for link in links:
        try:
            # Keycloak renders the broker link relative to the realm, so resolve
            # it against the current page before checking the origin.
            href = urlsplit(urljoin(page.url, link.get_attribute("href") or ""))
        except (AttributeError, TypeError, ValueError):
            continue
        try:
            query = parse_qs(href.query, keep_blank_values=True, strict_parsing=True)
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            href.scheme == "https"
            and href.hostname == "keys.urfu.ru"
            and href.port in (None, 443)
            and href.username is None
            and href.password is None
            and href.path == "/auth/realms/urfu-lk/broker/saml/login"
            and not href.fragment
            and set(query) <= allowed
            and all(len(values) == 1 and values[0] for values in query.values())
            and all(href.query.count(f"{key}=") == 1 for key in query)
            and link.get_attribute("target") in (None, "", "_self")
        ):
            matches.append(link)
    return matches[0] if len(matches) == 1 else None


def _wait_and_fill_saved_credentials(
    page: Any, credentials: CredentialRecord, timeout_error: type[Exception],
    *, istudent_flow: bool = False, elearn_flow: bool = False,
) -> None:
    """Follow verified federation, then fill the exact official ADFS form once.

    iStudent reaches ADFS through the Keycloak OpenID Connect endpoint; eLearn
    reaches the same ADFS form through the Keycloak SAML endpoint, so each
    flow follows its own verified link instead of guessing.
    """
    try:
        if elearn_flow and not page.url.startswith("https://sso.urfu.ru/adfs/ls/"):
            try:
                page.wait_for_url(
                    "https://keys.urfu.ru/auth/realms/urfu-lk/protocol/saml**",
                    timeout=15_000,
                )
            except timeout_error:
                pass
            link = _unique_keycloak_saml_broker_link(page)
            if link is None:
                # Host and path only: the query carries a SAMLRequest value.
                print(f"eLearn saml page: {_page_location(page)} broker_link=missing")
                raise ModeusAuthenticationError(
                    "Saved sign-in reached an unsupported eLearn identity-provider page"
                )
            link.click()
            # The broker click leads either to the ADFS form or, when a live
            # Keycloak SSO session already exists, straight back to eLearn.
            # Accept both instead of requiring the form to appear.
            adfs_form_seen = False
            give_up_at = time.monotonic() + 25.0
            while True:
                if page.url.startswith("https://sso.urfu.ru/adfs/ls/"):
                    if page.locator("form#loginForm input[name='UserName']").count():
                        adfs_form_seen = True
                        break
                elif _is_elearn_course_page(page.url):
                    break
                if time.monotonic() >= give_up_at:
                    break
                page.wait_for_timeout(500)
            if not adfs_form_seen and not _is_elearn_course_page(page.url):
                print(f"eLearn after broker click: {_page_location(page)} adfs_form=missing")
                raise ModeusAuthenticationError(
                    "Saved sign-in did not reach the URFU sign-in form"
                )
        elif istudent_flow and not _trusted_istudent_keycloak_authorization(page) and not page.url.startswith("https://sso.urfu.ru/adfs/ls/"):
            try:
                page.wait_for_url(
                    "https://keys.urfu.ru/auth/realms/urfu-lk/protocol/openid-connect/auth**",
                    timeout=15_000,
                )
            except timeout_error:
                pass
        if _trusted_istudent_keycloak_authorization(page):
            link = _trusted_istudent_federation_link(page)
            if link is None:
                raise ModeusAuthenticationError(
                    "Saved sign-in could not verify the official iStudent federation link"
                )
            link.click()
            page.wait_for_url("https://sso.urfu.ru/adfs/ls/**", timeout=25_000)
            page.wait_for_selector("form#loginForm input[name='UserName']", timeout=15_000)
        elif istudent_flow and _is_keys_identity_provider_page(page):
            raise ModeusAuthenticationError(
                "Saved sign-in reached an unsupported iStudent identity-provider page"
            )
        elif istudent_flow and page.url.startswith("https://sso.urfu.ru/adfs/ls/"):
            page.wait_for_selector("form#loginForm input[name='UserName']", timeout=15_000)
        elif istudent_flow:
            raise ModeusAuthenticationError("Saved sign-in is not on the trusted URFU ADFS form")
        else:
            page.wait_for_url("https://sso.urfu.ru/adfs/ls/**", timeout=15_000)
            page.wait_for_selector("form#loginForm input[name='UserName']", timeout=15_000)
    except ModeusAuthenticationError:
        raise
    except timeout_error:
        raise ModeusAuthenticationError(
            "Saved sign-in could not find the verified URFU password form; "
            "use browser sign-in if an additional step is required"
        ) from None
    if not _fill_saved_credentials(page, credentials):
        raise ModeusAuthenticationError(
            "Trusted URFU sign-in form could not be verified"
        )




def _wait_for_saved_modeus_session(
    page: Any, timeout_error: type[Exception],
) -> None:
    """Allow MFA, but reject any further password challenge without retrying."""
    expression = (
        "() => { const hasOidc = s => Object.keys(s).some("
        "key => key.startsWith('oidc.user:')); "
        "if (location.hostname === 'urfu.modeus.org' && "
        "(hasOidc(localStorage) || hasOidc(sessionStorage))) return 'authenticated'; "
        "if (location.protocol === 'https:' && location.hostname === 'sso.urfu.ru' "
        "&& location.pathname === '/adfs/ls/' && "
        "document.querySelectorAll('form#loginForm input[name=UserName]').length === 1 "
        "&& document.querySelectorAll('form#loginForm input[name=Password]').length === 1) "
        "return 'password'; return false; }"
    )
    try:
        outcome = page.wait_for_function(
            expression, timeout=_AUTH_TIMEOUT * 1000
        ).json_value()
    except timeout_error:
        raise ModeusAuthenticationError("Modeus SSO did not complete") from None
    if outcome != "authenticated":
        raise ModeusAuthenticationError(
            "Saved sign-in encountered an additional password challenge; "
            "choose browser sign-in for this account"
        )


def _fill_saved_credentials(page: Any, credentials: CredentialRecord) -> bool:
    """Fill and submit only the exact official HTTPS URFU ADFS login form."""
    parsed = urlsplit(page.url)
    try:
        trusted = (
            parsed.scheme == "https"
            and parsed.hostname == "sso.urfu.ru"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/adfs/ls/"
            and not parsed.fragment
        )
    except ValueError:
        trusted = False
    if not trusted:
        return False
    form = page.locator("form#loginForm")
    username = form.locator("input[name='UserName']")
    password = form.locator("input[name='Password']")
    if form.count() != 1 or username.count() != 1 or password.count() != 1:
        return False
    if not _trusted_adfs_form_action(page, form):
        return False
    submit = form.locator("button[type='submit'], input[type='submit'], #submitButton")
    if submit.count() != 1:
        raise ModeusAuthenticationError("Trusted URFU sign-in form has no unique submit control")
    username.fill(credentials.email)
    password.fill(credentials.password)
    submit.click()
    return True


def _trusted_https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and port in (None, 443)
        and parsed.hostname in _TRUSTED_METADATA_HOSTS
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _trusted_issuer_url(value: Any) -> bool:
    return isinstance(value, str) and value in {
        TRUSTED_OIDC_ISSUER,
        _TRUSTED_MODEUS_TOKEN_ISSUER,
    }


def _safe_issuer_location(value: str) -> str:
    """Describe only the issuer URL's public location, never credentials or query data."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "invalid URL"
    if not hostname:
        return "unknown host"
    authority = hostname
    if port is not None:
        authority = f"{authority}:{port}"
    path = parsed.path[:128]
    return f"{parsed.scheme}://{authority}{path}"


def _read_unverified_token_issuer(token: Any) -> str | None:
    """Read JWT `iss` for candidate selection; signature verification must follow."""
    if not isinstance(token, str) or len(token) > 65_536:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    try:
        payload = base64.b64decode(
            payload_segment + "=" * (-len(payload_segment) % 4),
            altchars=b"-_",
            validate=True,
        )
        claims = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    issuer = claims.get("iss") if isinstance(claims, dict) else None
    return issuer if isinstance(issuer, str) else None


def _unverified_token_issuer(token: Any) -> str | None:
    issuer = _read_unverified_token_issuer(token)
    return _safe_issuer_location(issuer) if issuer is not None else None
