"""Command-line entry point for configuring URFU authentication."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from urfu_mcp.auth.credential_store import (
    CredentialRecord,
    CredentialStoreError,
    create_credential_store,
)
from urfu_mcp.auth.modeus_browser import run_modeus_login
from urfu_mcp.auth.oidc import OidcConfig
from urfu_mcp.auth.oidc_cli import run_login
from urfu_mcp.config import (
    ConfigError,
    initialize_config,
    load_config,
    update_auth_settings,
)
from urfu_mcp.setup import run_setup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="urfu-mcp")
    commands = parser.add_subparsers(dest="command")
    setup_command = commands.add_parser("setup", help="configure and verify URFU-MCP")
    setup_command.add_argument("--no-color", action="store_true", help="disable terminal colors")
    setup_command.add_argument("--non-interactive", action="store_true", help="skip terminal confirmation")
    commands.add_parser("init", help="create config.yaml with safe defaults")
    commands.add_parser("start", help="run the managed MCP stdio server")
    commands.add_parser("stop", help="stop only the recorded managed MCP process")
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
        print("config.yaml is ready. Run `urfu-mcp auth` to sign in.")
        return 0

    if options.command == "setup":
        return run_setup(
            no_color=options.no_color,
            non_interactive=options.non_interactive,
        )

    if options.command in {"start", "stop"}:
        from urfu_mcp import process_manager

        return process_manager.start() if options.command == "start" else process_manager.stop()

    if options.command == "serve":
        from urfu_mcp.runtime import serve

        return serve()

    if auth_requested and options.command is None:
        try:
            load_config()
        except ConfigError:
            print("Could not read config.yaml safely.", file=sys.stderr)
            return 1
        return 0 if run_modeus_login() else 1

    if options.command == "oidc":
        try:
            loaded = load_config()
            settings = loaded.auth
        except ConfigError:
            print("Could not read config.yaml safely.", file=sys.stderr)
            return 1

        issuer = settings.issuer
        client_id = settings.client_id
        try:
            if not issuer:
                issuer = input("OIDC issuer: ").strip()
                if not issuer:
                    print("OIDC issuer must not be empty.", file=sys.stderr)
                    return 1
            if not client_id:
                client_id = input("OIDC client_id: ").strip()
                if not client_id:
                    print("OIDC client_id must not be empty.", file=sys.stderr)
                    return 1
        except (EOFError, KeyboardInterrupt):
            print("OIDC settings entry cancelled.", file=sys.stderr)
            return 1

        try:
            config = OidcConfig(
                issuer=issuer,
                client_id=client_id,
                redirect_uri=settings.redirect_uri,
                person_id_claim=settings.person_id_claim,
                callback_timeout_seconds=settings.callback_timeout_seconds,
                metadata_timeout_seconds=settings.metadata_timeout_seconds,
                metadata_max_bytes=settings.metadata_max_bytes,
                transaction_ttl_seconds=settings.transaction_ttl_seconds,
            )
        except ValueError:
            print("Invalid OIDC provider settings.", file=sys.stderr)
            return 1

        if not settings.issuer or not settings.client_id:
            try:
                update_auth_settings(issuer=issuer, client_id=client_id)
            except ConfigError:
                print("Could not save OIDC settings securely.", file=sys.stderr)
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
