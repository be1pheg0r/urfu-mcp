"""Shared interactive authentication method selector."""

from __future__ import annotations

import getpass
import sys

import questionary

from urfu_mcp.auth.credential_store import (
    CredentialRecord,
    CredentialStoreError,
    create_credential_store,
)

MANUAL_METHOD = "OAuth (browser sign-in)"
SAVED_METHOD = "Stashed credentials (automatic SSO)"


def choose_authentication_method() -> str | None:
    """Choose via an arrow-key TTY picker, failing closed without a terminal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        selected = questionary.select(
            "Choose a sign-in method",
            choices=[MANUAL_METHOD, SAVED_METHOD],
            instruction="Use ↑/↓ and Enter",
        ).ask()
    except (EOFError, KeyboardInterrupt):
        return None
    return {MANUAL_METHOD: "manual", SAVED_METHOD: "saved"}.get(selected)


def prepare_saved_credentials() -> bool:
    """Reuse keyring credentials or collect/save them locally once."""
    try:
        store = create_credential_store()
        if store.load_any() is None:
            email = input("Email: ").strip()
            if not email:
                return False
            password = getpass.getpass("Password: ")
            if not password.strip():
                return False
            store.save(CredentialRecord(email=email, password=password))
    except (CredentialStoreError, EOFError, KeyboardInterrupt):
        return False
    return True
