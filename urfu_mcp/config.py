"""Generate and validate the per-user YAML runtime configuration."""

from __future__ import annotations

import os
import secrets
from copy import deepcopy
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"name": "urfu-mcp"},
    "auth": {
        "issuer": None,
        "client_id": None,
        "redirect_uri": "http://127.0.0.1:43123/callback",
        "person_id_claim": "person_id",
        "token_kind": None,
        "callback_timeout_seconds": 300,
        "metadata_timeout_seconds": 10,
        "metadata_max_bytes": 1_000_000,
        "transaction_ttl_seconds": 600,
    },
    "sidecar": {
        "base_url": "http://127.0.0.1:8080",
        "api_key": None,
        "timeout_seconds": 10.0,
        "max_response_bytes": 2_000_000,
        "event_page_size": 1000,
        "person_page_size": 50,
        "person_max_pages": 10,
    },
    "schedule": {
        "timezone": "Asia/Yekaterinburg",
        "max_days": 14,
        "max_subjects": 3,
    },
}


class ConfigError(ValueError):
    """A safe, non-value-bearing configuration error."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="allow")


class ServerSettings(_Section):
    name: str = "urfu-mcp"

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("server name is required")
        return value.strip()


class AuthSettings(_Section):
    issuer: str | None = None
    client_id: str | None = None
    redirect_uri: str = "http://127.0.0.1:43123/callback"
    person_id_claim: str = "person_id"
    token_kind: str | None = None
    callback_timeout_seconds: int = Field(default=300, ge=1, le=1800)
    metadata_timeout_seconds: float = Field(default=10, gt=0, le=60)
    metadata_max_bytes: int = Field(default=1_000_000, ge=1024, le=10_000_000)
    transaction_ttl_seconds: int = Field(default=600, ge=30, le=3600)

    @field_validator("issuer", "client_id", mode="before")
    @classmethod
    def empty_optional_string_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("person_id_claim")
    @classmethod
    def claim_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("claim name is required")
        return value.strip()

    @field_validator("token_kind")
    @classmethod
    def token_kind_must_be_supported(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in {"id_token", "access_token"}:
            raise ValueError("token kind is unsupported")
        return value


class SidecarSettings(_Section):
    base_url: str = "http://127.0.0.1:8080"
    api_key: SecretStr
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    max_response_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    event_page_size: int = Field(default=1000, ge=1, le=5000)
    person_page_size: int = Field(default=50, ge=1, le=100)
    person_max_pages: int = Field(default=10, ge=1, le=100)

    @field_validator("api_key")
    @classmethod
    def api_key_must_be_nonempty(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not secret or any(character.isspace() for character in secret):
            raise ValueError("sidecar API key must be non-empty and contain no whitespace")
        return value


class ScheduleSettings(_Section):
    timezone: str = "Asia/Yekaterinburg"
    max_days: int = Field(default=14, ge=1, le=31)
    max_subjects: int = Field(default=3, ge=1, le=10)

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone must be a valid IANA timezone") from None
        return value


class AppConfig(_Section):
    server: ServerSettings = Field(default_factory=ServerSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    sidecar: SidecarSettings
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)

    @property
    def server_name(self) -> str:
        return self.server.name


def load_config(path: str | os.PathLike[str] = "config.yaml") -> AppConfig:
    """Load config, creating it with secure defaults and a local API key if absent."""
    config_path = Path(path)
    document = _read_or_default(config_path)
    defaults = deepcopy(DEFAULT_CONFIG)
    defaults["sidecar"]["api_key"] = secrets.token_urlsafe(32)

    changed = _merge_missing(document, defaults)
    if changed:
        _write_config(config_path, document)
    else:
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            raise ConfigError("Could not secure config.yaml permissions") from None

    try:
        return AppConfig.model_validate(document)
    except ValidationError:
        raise ConfigError("config.yaml contains invalid or unsupported settings") from None


def initialize_config(path: str | os.PathLike[str] = "config.yaml") -> Path:
    """Create or validate config.yaml without printing any configured values."""
    config_path = Path(path)
    load_config(config_path)
    return config_path


def update_auth_settings(
    path: str | os.PathLike[str] = "config.yaml",
    *,
    issuer: str,
    client_id: str,
) -> AppConfig:
    """Atomically update public OIDC settings without replacing other YAML values."""
    config_path = Path(path)
    load_config(config_path)
    document = _read_or_default(config_path)
    auth = document.get("auth")
    if not isinstance(auth, dict):
        raise ConfigError("config.yaml contains invalid or unsupported settings")
    auth["issuer"] = issuer
    auth["client_id"] = client_id
    try:
        validated = AppConfig.model_validate(document)
    except ValidationError:
        raise ConfigError("config.yaml contains invalid or unsupported settings") from None
    _write_config(config_path, document)
    return validated


def _read_or_default(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ConfigError("config.yaml must not be a symbolic link")
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("config.yaml could not be read as safe YAML") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("config.yaml must contain a YAML mapping")
    return loaded


def _merge_missing(target: dict[str, Any], defaults: dict[str, Any]) -> bool:
    changed = False
    for key, default_value in defaults.items():
        if key not in target or key == "api_key" and not target[key]:
            target[key] = deepcopy(default_value)
            changed = True
        elif isinstance(default_value, dict) and isinstance(target[key], dict):
            changed = _merge_missing(target[key], default_value) or changed
    return changed


def _write_config(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(document, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except (OSError, yaml.YAMLError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigError("config.yaml could not be created securely") from None


__all__ = [
    "DEFAULT_CONFIG",
    "AppConfig",
    "AuthSettings",
    "ConfigError",
    "ScheduleSettings",
    "ServerSettings",
    "SidecarSettings",
    "initialize_config",
    "load_config",
    "update_auth_settings",
]
