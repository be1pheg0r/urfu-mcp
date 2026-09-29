"""Encrypted OS-keyring storage for identity-bound eLearn sessions."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from urfu_mcp.auth.credential_store import PasswordBackend, create_password_backend

_PART_SIZE = 1000
_MAX_PAYLOAD = 100_000
_MAX_PARTS = 100
_MANIFEST_PREFIX = "elearn-session-v1:"


class ELearnSessionStoreError(ValueError):
    """Safe failure reading or writing an eLearn session record."""


@dataclass(frozen=True, slots=True)
class ELearnSessionRecord:
    """Moodle session cookie bound to a selected Modeus person."""

    modeus_person_id: str
    moodle_session_id: str = field(repr=False)
    sesskey: str | None = field(default=None, repr=False)
    username: str | None = None
    expires_at: float = 0.0
    origin: str = "https://elearn.urfu.ru"
    path: str = "/"


class ELearnSessionStore:
    """Persist eLearn session records in the OS keyring."""

    def __init__(self, backend: PasswordBackend, *, service_name: str = "urfu-mcp.elearn", account: str = "default") -> None:
        self._backend = backend
        self._service_name = service_name
        self._account = account

    def save(self, record: ELearnSessionRecord) -> None:
        _validate(record, now=time.time())
        try:
            payload = json.dumps({
                "modeus_person_id": record.modeus_person_id,
                "moodle_session_id": record.moodle_session_id,
                "sesskey": record.sesskey,
                "username": record.username,
                "expires_at": record.expires_at,
                "origin": record.origin,
                "path": record.path,
            }, separators=(",", ":"), allow_nan=False, ensure_ascii=True)
            if len(payload) > _MAX_PAYLOAD:
                raise ELearnSessionStoreError("eLearn session is too large to store securely")
            parts = [payload[i:i + _PART_SIZE] for i in range(0, len(payload), _PART_SIZE)]
            if not parts or len(parts) > _MAX_PARTS:
                raise ELearnSessionStoreError("eLearn session is too large to store securely")
            generation = uuid4().hex
            previous = self._read_manifest_generation()
            written: list[str] = []
            try:
                for index, part in enumerate(parts):
                    account = f"part-{generation}-{index}"
                    self._backend.set_password(self._service_name, account, part)
                    written.append(account)
                manifest = json.dumps({
                    "version": 1, "generation": generation, "count": len(parts),
                    "sha256": hashlib.sha256(payload.encode("ascii")).hexdigest(),
                }, separators=(",", ":"), sort_keys=True)
                self._backend.set_password(self._service_name, self._account, _MANIFEST_PREFIX + manifest)
            except Exception:  # noqa: BLE001
                for account in written:
                    try:
                        self._backend.delete_password(self._service_name, account)
                    except Exception:  # noqa: BLE001,S110 - best-effort cleanup
                        pass
                raise ELearnSessionStoreError("Could not save eLearn session securely") from None
            if previous is not None:
                old_generation, old_count = previous
                for index in range(old_count):
                    try:
                        self._backend.delete_password(self._service_name, f"part-{old_generation}-{index}")
                    except Exception:  # noqa: BLE001,S110 - best-effort cleanup
                        pass
        except ELearnSessionStoreError:
            raise
        except Exception:  # noqa: BLE001
            raise ELearnSessionStoreError("Could not save eLearn session securely") from None

    def load(self, modeus_person_id: str) -> ELearnSessionRecord | None:
        try:
            encoded = self._backend.get_password(self._service_name, self._account)
        except Exception:  # noqa: BLE001
            raise ELearnSessionStoreError("Could not load eLearn session securely") from None
        if encoded is None:
            return None
        try:
            if not encoded.startswith(_MANIFEST_PREFIX):
                raise ValueError
            manifest: Any = json.loads(encoded[len(_MANIFEST_PREFIX):])
            if (not isinstance(manifest, dict)
                    or set(manifest) != {"version", "generation", "count", "sha256"}
                    or manifest["version"] != 1
                    or not isinstance(manifest["generation"], str)
                    or len(manifest["generation"]) != 32
                    or any(char not in "0123456789abcdef" for char in manifest["generation"])
                    or type(manifest["count"]) is not int
                    or not 1 <= manifest["count"] <= _MAX_PARTS
                    or not isinstance(manifest["sha256"], str)
                    or len(manifest["sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in manifest["sha256"])):
                raise ValueError
            try:
                chunks = [self._backend.get_password(self._service_name, f"part-{manifest['generation']}-{i}")
                          for i in range(manifest["count"])]
            except Exception:  # noqa: BLE001
                raise ELearnSessionStoreError("Could not load eLearn session securely") from None
            if any(chunk is None or len(chunk) > _PART_SIZE for chunk in chunks):
                raise ValueError
            payload = "".join(chunk for chunk in chunks if chunk is not None)
            if len(payload) > _MAX_PAYLOAD or hashlib.sha256(payload.encode("ascii")).hexdigest() != manifest["sha256"]:
                raise ValueError
            data: Any = json.loads(payload)
            fields = {"modeus_person_id", "moodle_session_id", "sesskey", "username", "expires_at", "origin", "path"}
            if not isinstance(data, dict) or set(data) != fields:
                raise ValueError
            record = ELearnSessionRecord(**data)
            _validate(record, now=time.time())
        except ELearnSessionStoreError:
            raise
        except (TypeError, ValueError, KeyError, UnicodeError):
            raise ELearnSessionStoreError("Stored eLearn session is invalid or expired") from None
        if record.modeus_person_id != modeus_person_id:
            raise ELearnSessionStoreError("Stored eLearn session belongs to a different Modeus person")
        return record

    def delete(self) -> None:
        try:
            encoded = self._backend.get_password(self._service_name, self._account)
            if encoded and encoded.startswith(_MANIFEST_PREFIX):
                try:
                    manifest: Any = json.loads(encoded[len(_MANIFEST_PREFIX):])
                    generation = manifest.get("generation") if isinstance(manifest, dict) else None
                    count = manifest.get("count") if isinstance(manifest, dict) else None
                    if (_valid_generation(generation) and type(count) is int and 0 <= count <= _MAX_PARTS):
                        for index in range(count):
                            try:
                                self._backend.delete_password(self._service_name, f"part-{generation}-{index}")
                            except Exception:  # noqa: BLE001,S110 - best-effort deletion
                                pass
                except (ValueError, TypeError):
                    pass
            self._backend.delete_password(self._service_name, self._account)
        except Exception:  # noqa: BLE001
            raise ELearnSessionStoreError("Could not delete eLearn session") from None

    def _read_manifest_generation(self) -> tuple[str, int] | None:
        try:
            encoded = self._backend.get_password(self._service_name, self._account)
            if not encoded or not encoded.startswith(_MANIFEST_PREFIX):
                return None
            manifest: Any = json.loads(encoded[len(_MANIFEST_PREFIX):])
            generation = manifest.get("generation") if isinstance(manifest, dict) else None
            count = manifest.get("count") if isinstance(manifest, dict) else None
            if _valid_generation(generation) and type(count) is int and 1 <= count <= _MAX_PARTS:
                return cast(str, generation), count
        except Exception:  # noqa: BLE001
            return None
        return None


def _valid_generation(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def create_elearn_session_store() -> ELearnSessionStore:
    """Create an eLearn session store using the selected secure OS keyring."""
    return ELearnSessionStore(create_password_backend())


def _validate(record: ELearnSessionRecord, *, now: float) -> None:
    try:
        person = UUID(record.modeus_person_id)
        if person.int == 0 or str(person) != record.modeus_person_id:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise ELearnSessionStoreError("eLearn session identity is invalid") from None
    if not isinstance(record.moodle_session_id, str) or not record.moodle_session_id.strip() or len(record.moodle_session_id) > 1024:
        raise ELearnSessionStoreError("eLearn session identifier is invalid")
    if record.sesskey is not None and (not isinstance(record.sesskey, str) or not record.sesskey.strip() or len(record.sesskey) > 1024):
        raise ELearnSessionStoreError("eLearn sesskey is invalid")
    if record.username is not None and (not isinstance(record.username, str) or len(record.username) > 512):
        raise ELearnSessionStoreError("eLearn username is invalid")
    if not isinstance(record.expires_at, (int, float)) or isinstance(record.expires_at, bool) or not math.isfinite(record.expires_at) or record.expires_at <= now:
        raise ELearnSessionStoreError("eLearn session expiry is invalid")
    try:
        parsed = urlsplit(record.origin)
        valid_origin = parsed.scheme == "https" and parsed.netloc == "elearn.urfu.ru" and not parsed.path and not parsed.query and not parsed.fragment
    except (TypeError, ValueError):
        valid_origin = False
    if not valid_origin or record.path != "/":
        raise ELearnSessionStoreError("eLearn session origin is invalid")


__all__ = ["ELearnSessionRecord", "ELearnSessionStore", "ELearnSessionStoreError", "create_elearn_session_store"]
