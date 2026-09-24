# urfu-mcp

## Local Modeus MCP runtime

Install Python dependencies once with `uv sync --all-groups`; .NET and the SfeduSchedule submodule are not required. Setup:

1. `uv run urfu-mcp init`
2. `uv run urfu-mcp auth`
3. `uv run urfu-mcp start`

`auth` opens Chromium on the Modeus sign-in page. Complete URFU SSO/MFA there. The CLI does not ask for issuer, client ID, token kind, email, or password. If a URFU account is in the `urfu-mcp` OS keyring entry, it can fill the SSO form; otherwise sign in in the browser.

After sign-in, the client reads the Modeus SPA's `oidc.user:*` session entry. It captures trusted Modeus app config or matching OIDC discovery metadata, then validates the ID-token signature, issuer, audience, expiry, and `person_id`. Modeus may provide only an ID token; absent access tokens remain absent. Tokens and identity are stored in the OS keyring. Authentication fails closed if trusted metadata is not observed.

The two schedule operations make bounded, in-process HTTPS requests directly to the fixed `https://urfu.modeus.org` calendar and person-search routes. Each call supplies its selected per-call Bearer token, enforces response-size/time limits, maps upstream failures safely, and requires complete pagination before person selection. There is no general proxy or arbitrary destination setting. Existing `sidecar` config entries from older versions are ignored; `modeus_http` controls request bounds.

`urfu-mcp start` runs the MCP stdio server in the foreground for the launching MCP client. Running it in a standalone terminal does not create a background MCP endpoint. `urfu-mcp stop` from another terminal signals only the recorded MCP process after checking its process creation time. State is private at `~/.urfu-mcp/processes.json`. Windows and WSL process/keyring state are separate; run the MCP client and authentication in the same environment.

`urfu-mcp auth oidc` remains the explicit generic PKCE flow. `urfu-mcp auth credentials` remains available for saving an account to the OS keyring.

Contract and authentication tests use synthetic data. A direct authenticated Modeus schedule query previously returned HTTP 200, but the new Python runtime itself has not been live-integrated against Modeus.
