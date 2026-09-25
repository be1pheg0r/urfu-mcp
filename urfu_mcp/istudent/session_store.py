"""Encrypted OS-keyring storage for identity-bound iStudent browser sessions."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from urfu_mcp.auth.credential_store import PasswordBackend, create_password_backend

_PART_SIZE = 1000
_MAX_PAYLOAD = 100_000
_MAX_PARTS = 100
_MANIFEST_PREFIX = "istudent-session-v1:"


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
        payload = json.dumps({
            "modeus_person_id": record.modeus_person_id,
            "istudent_subject": record.istudent_subject,
            "access_token": record.access_token,
            "php_session_id": record.php_session_id,
            "expires_at": record.expires_at,
            "origin": record.origin,
            "path": record.path,
        }, separators=(",", ":"), allow_nan=False, ensure_ascii=True)
        if len(payload) > _MAX_PAYLOAD:
            raise IStudentSessionStoreError("iStudent session is too large to store securely")
        parts = [payload[i:i + _PART_SIZE] for i in range(0, len(payload), _PART_SIZE)]
        if not parts or len(parts) > _MAX_PARTS:
            raise IStudentSessionStoreError("iStudent session is too large to store securely")
        generation = uuid4().hex
        previous_manifest = self._read_manifest_generation()
        accounts = [f"part-{generation}-{i}" for i in range(len(parts))]
        written: list[str] = []
        try:
            for account, part in zip(accounts, parts):
                self._backend.set_password(self._service_name, account, part)
                written.append(account)
            manifest = json.dumps({"version": 1, "generation": generation, "count": len(parts),
                                   "sha256": hashlib.sha256(payload.encode("ascii")).hexdigest()},
                                  separators=(",", ":"), sort_keys=True)
            self._backend.set_password(self._service_name, self._account, _MANIFEST_PREFIX + manifest)
            if previous_manifest is not None:
                old_generation, old_count = previous_manifest
                for i in range(old_count):
                    try:
                        self._backend.delete_password(self._service_name, f"part-{old_generation}-{i}")
                    except Exception:  # noqa: BLE001,S110 - best-effort stale generation cleanup
                        pass
        except Exception:  # noqa: BLE001 - backend errors may contain secret values
            for account in written:
                try:
                    self._backend.delete_password(self._service_name, account)
                except Exception:  # noqa: BLE001,S110 - best-effort cleanup
                    pass
            raise IStudentSessionStoreError("Could not save iStudent session securely") from None

    def load(self, modeus_person_id: str) -> IStudentSessionRecord | None:
        try:
            encoded = self._backend.get_password(self._service_name, self._account)
        except Exception:  # noqa: BLE001 - backend errors may contain secret values
            raise IStudentSessionStoreError("Could not load iStudent session securely") from None
        if encoded is None:
            return None
        try:
            if encoded.startswith(_MANIFEST_PREFIX):
                manifest: Any = json.loads(encoded[len(_MANIFEST_PREFIX):])
                if (not isinstance(manifest, dict) or set(manifest) != {"version", "generation", "count", "sha256"}
                        or manifest["version"] != 1 or not isinstance(manifest["generation"], str)
                        or len(manifest["generation"]) != 32 or any(c not in "0123456789abcdef" for c in manifest["generation"])
                        or type(manifest["count"]) is not int or not 1 <= manifest["count"] <= _MAX_PARTS
                        or not isinstance(manifest["sha256"], str) or len(manifest["sha256"]) != 64
                        or any(c not in "0123456789abcdef" for c in manifest["sha256"])):
                    raise ValueError
                chunks = [self._backend.get_password(self._service_name, f"part-{manifest['generation']}-{i}")
                          for i in range(manifest["count"])]
                if any(chunk is None or len(chunk) > _PART_SIZE for chunk in chunks):
                    raise ValueError
                payload = "".join(chunk for chunk in chunks if chunk is not None)
                if len(payload) > _MAX_PAYLOAD or hashlib.sha256(payload.encode("ascii")).hexdigest() != manifest["sha256"]:
                    raise ValueError
                encoded = payload
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
            encoded = self._backend.get_password(self._service_name, self._account)
            if encoded and encoded.startswith(_MANIFEST_PREFIX):
                try:
                    manifest: Any = json.loads(encoded[len(_MANIFEST_PREFIX):])
                    generation = manifest.get("generation") if isinstance(manifest, dict) else None
                    count = manifest.get("count") if isinstance(manifest, dict) else None
                    if (isinstance(generation, str) and len(generation) == 32 and all(c in "0123456789abcdef" for c in generation)
                            and type(count) is int and 0 <= count <= _MAX_PARTS):
                        for i in range(count):
                            try:
                                self._backend.delete_password(self._service_name, f"part-{generation}-{i}")
                            except Exception:  # noqa: BLE001,S110 - best-effort deletion
                                pass
                except (ValueError, TypeError):
                    pass
            self._backend.delete_password(self._service_name, self._account)
        except Exception:  # noqa: BLE001 - backend errors may contain secret values
            raise IStudentSessionStoreError("Could not delete iStudent session") from None

    def _read_manifest_generation(self) -> tuple[str, int] | None:
        try:
            encoded = self._backend.get_password(self._service_name, self._account)
            if not encoded or not encoded.startswith(_MANIFEST_PREFIX):
                return None
            manifest: Any = json.loads(encoded[len(_MANIFEST_PREFIX):])
            generation = manifest.get("generation") if isinstance(manifest, dict) else None
            count = manifest.get("count") if isinstance(manifest, dict) else None
            if (isinstance(generation, str) and len(generation) == 32
                    and all(c in "0123456789abcdef" for c in generation)
                    and type(count) is int and 1 <= count <= _MAX_PARTS):
                return generation, count
        except Exception:  # noqa: BLE001 - malformed prior state is left untouched
            return None
        return None


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
