"""Encrypted OS-keyring storage for identity-bound iStudent browser sessions."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from urfu_mcp.auth.credential_store import PasswordBackend, create_password_backend


class IStudentSessionStoreError(ValueError):
    """Safe failure reading or writing an iStudent session record."""


@dataclass(frozen=True, slots=True)
class IStudentSessionRecord:
    """Cookie pair tied to both signed iStudent subject and selected Modeus person."""

    modeus_person_id: str
    istudent_subject: str
    access_token: str = field(repr=False)
    php_session_id: str = field(repr=False)
    expires_at: float
    origin: str = "https://istudent.urfu.ru"
    path: str = "/"


class IStudentSessionStore:
    """Persist and retrieve a narrowly scoped iStudent cookie pair in OS keyring."""

    def __init__(self, backend: PasswordBackend, *, service_name: str = "urfu-mcp.istudent", account: str = "default"):
        self._backend = backend
        self._service_name = service_name
        self._account = account

    def save(self, record: IStudentSessionRecord) -> None:
        _validate(record, now=time.time())
        try:
            self._backend.set_password(self._service_name, self._account, json.dumps({
                "modeus_person_id": record.modeus_person_id,
                "istudent_subject": record.istudent_subject,
                "access_token": record.access_token,
                "php_session_id": record.php_session_id,
                "expires_at": record.expires_at,
                "origin": record.origin,
                "path": record.path,
            }, separators=(",", ":"), allow_nan=False))
        except Exception:  # noqa: BLE001 - backend errors may contain secret values
            raise IStudentSessionStoreError("Could not save iStudent session securely") from None

    def load(self, modeus_person_id: str) -> IStudentSessionRecord | None:
        try:
            encoded = self._backend.get_password(self._service_name, self._account)
        except Exception:  # noqa: BLE001 - backend errors may contain secret values
            raise IStudentSessionStoreError("Could not load iStudent session securely") from None
        if encoded is None:
            return None
        try:
            data: Any = json.loads(encoded)
            fields = {"modeus_person_id", "istudent_subject", "access_token", "php_session_id", "expires_at", "origin", "path"}
            if not isinstance(data, dict) or set(data) != fields:
                raise ValueError
            record = IStudentSessionRecord(**data)
            _validate(record, now=time.time())
        except (TypeError, ValueError, KeyError):
            raise IStudentSessionStoreError("Stored iStudent session is invalid or expired") from None
        if record.modeus_person_id != modeus_person_id:
            raise IStudentSessionStoreError("Stored iStudent session belongs to a different Modeus person")
        return record

    def delete(self) -> None:
        try:
            self._backend.delete_password(self._service_name, self._account)
        except Exception:  # noqa: BLE001 - backend errors may contain secret values
            raise IStudentSessionStoreError("Could not delete iStudent session") from None


def create_istudent_session_store() -> IStudentSessionStore:
    return IStudentSessionStore(create_password_backend())


def _validate(record: IStudentSessionRecord, *, now: float, check_expiry: bool = True) -> None:
    try:
        person = UUID(record.modeus_person_id)
        if person.int == 0 or str(person) != record.modeus_person_id:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise IStudentSessionStoreError("iStudent session identity is invalid") from None
    if not isinstance(record.istudent_subject, str) or not record.istudent_subject.strip() or len(record.istudent_subject) > 512:
        raise IStudentSessionStoreError("iStudent signed subject is invalid")
    if not isinstance(record.access_token, str) or record.access_token.count(".") != 2 or len(record.access_token) > 65_536:
        raise IStudentSessionStoreError("iStudent signed token is invalid")
    if not isinstance(record.php_session_id, str) or not record.php_session_id or len(record.php_session_id) > 1024:
        raise IStudentSessionStoreError("iStudent session identifier is invalid")
    if not isinstance(record.expires_at, (int, float)) or isinstance(record.expires_at, bool) or not math.isfinite(record.expires_at):
        raise IStudentSessionStoreError("iStudent session expiry is invalid")
    if check_expiry and record.expires_at <= now:
        raise IStudentSessionStoreError("iStudent session is expired")
    parsed = urlsplit(record.origin)
    if parsed.scheme != "https" or parsed.netloc != "istudent.urfu.ru" or parsed.path or parsed.query or parsed.fragment or record.path != "/":
        raise IStudentSessionStoreError("iStudent session origin is invalid")
