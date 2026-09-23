# urfu-mcp

## Local Modeus MCP runtime

`urfu-mcp serve` runs the existing schedule MCP tools over stdio. It does not start SfeduSchedule; configure a separately running local sidecar. The server exposes `retrieve_user_schedule` and `retrieve_person_schedule`. The latter permits a person ID only after the existing complete person search resolves it; explicitly selecting a foreign match is permitted by the default policy.

Set all of these environment variables before running:

- `URFU_MCP_PERSON_ID`: explicit, non-nil UUID used as the signed-in Modeus identity. It is not inferred from an OIDC claim.
- `URFU_MCP_MODEUS_TOKEN_KIND`: exactly `id_token` or `access_token`; the selected token is read only from the system token store created by `urfu-mcp auth oidc`.
- `URFU_MCP_SFEDU_URL`: loopback HTTP(S) base URL for an already-running SfeduSchedule service.
- `URFU_MCP_API_KEY`: API key used only for the local person-search route.
- `URFU_MCP_MAX_DAYS`: explicit inclusive schedule period limit, integer 1–31.
- `URFU_MCP_MAX_SUBJECTS`: explicit people-per-request limit, integer 1–10.

Startup fails closed if required settings are missing or invalid, or if the selected system-stored token has no expiry or is expired. The token expiry is checked again for each tool call. Tokens and the API key are not printed. `urfu-mcp auth credentials` continues to store credentials only; this runtime does not perform a password login or exchange those credentials for a session.

The runtime wiring and requests have been verified with synthetic keyring/token data and an in-memory HTTP transport. No live URFU/Modeus/SfeduSchedule integration is claimed or required by these checks. The chosen token kind must be explicitly selected by the operator; local tests do not establish which token a particular deployment accepts.
