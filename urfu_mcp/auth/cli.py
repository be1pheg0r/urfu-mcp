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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="urfu-mcp")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("credentials", help="store email and password securely")
    oidc = commands.add_parser(
        "oidc", help="sign in with explicit OIDC provider settings"
    )
    oidc.add_argument("--issuer", required=True)
    oidc.add_argument("--client-id", required=True)
    oidc.add_argument("--redirect-uri", required=True)
    commands.add_parser(
        "serve", help="run the single-user Modeus MCP server over stdio"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the auth command and return a process exit status."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "auth":
        arguments.pop(0)

    parser = _parser()
    try:
        options = parser.parse_args(arguments)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if options.command == "serve":
        from urfu_mcp.runtime import serve

        return serve()

    if options.command == "oidc":
        try:
            config = OidcConfig(
                issuer=options.issuer,
                client_id=options.client_id,
                redirect_uri=options.redirect_uri,
            )
        except ValueError:
            print("Invalid OIDC provider settings.", file=sys.stderr)
            return 1
        return 0 if run_login(config) else 1

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
