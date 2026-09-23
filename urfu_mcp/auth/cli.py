"""Command-line entry point for configuring URFU authentication."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from urfu_mcp.auth.credential_store import (CredentialRecord,
                                            CredentialStoreError,
                                            create_credential_store)
from urfu_mcp.auth.oidc import OidcConfig
from urfu_mcp.auth.oidc_cli import run_login
from urfu_mcp.config import ConfigError, initialize_config, load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="urfu-mcp")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("init", help="create config.yaml with safe defaults")
    commands.add_parser("credentials", help="store email and password securely")
    commands.add_parser(
        "oidc", help="sign in using OIDC settings from config.yaml"
    )
    commands.add_parser(
        "serve", help="run the single-user Modeus MCP server over stdio"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the auth command and return a process exit status."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    auth_requested = bool(arguments and arguments[0] == "auth")
    if auth_requested:
        arguments.pop(0)

    parser = _parser()
    try:
        options = parser.parse_args(arguments)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if options.command == "init":
        try:
            initialize_config()
        except ConfigError:
            print("Could not create or validate config.yaml securely.", file=sys.stderr)
            return 1
        print("config.yaml is ready. Review its settings before authentication.")
        return 0

    if options.command == "serve":
        from urfu_mcp.runtime import serve

        return serve()

    if options.command == "oidc" or (auth_requested and options.command is None):
        try:
            settings = load_config().auth
        except ConfigError:
            print("Could not read config.yaml safely.", file=sys.stderr)
            return 1
        if not settings.issuer or not settings.client_id:
            print(
                "Set auth.issuer and auth.client_id in config.yaml to the registered OIDC provider values.",
                file=sys.stderr,
            )
            return 1
        try:
            config = OidcConfig(
                issuer=settings.issuer,
                client_id=settings.client_id,
                redirect_uri=settings.redirect_uri,
                person_id_claim=settings.person_id_claim,
                callback_timeout_seconds=settings.callback_timeout_seconds,
                metadata_timeout_seconds=settings.metadata_timeout_seconds,
                metadata_max_bytes=settings.metadata_max_bytes,
                transaction_ttl_seconds=settings.transaction_ttl_seconds,
            )
        except ValueError:
            print("Invalid OIDC settings in config.yaml.", file=sys.stderr)
            return 1
        return 0 if run_login(config) else 1

    if options.command is None:
        parser.print_help()
        return 0

    try:
        email = input("Email: ").strip()
        if not email:
            print("Email must not be empty.", file=sys.stderr)
            return 1
        password = getpass.getpass("Password: ")
        if not password.strip():
            print("Password must not be empty.", file=sys.stderr)
            return 1
        create_credential_store().save(CredentialRecord(email=email, password=password))
    except CredentialStoreError:
        print("Could not store credentials in the system keyring.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Credential entry cancelled.", file=sys.stderr)
        return 1

    print("Credentials stored securely.")
    return 0


def entrypoint() -> None:
    """Console-script adapter."""
    raise SystemExit(main())
