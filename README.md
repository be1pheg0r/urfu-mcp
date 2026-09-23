# urfu-mcp

## Local Modeus MCP runtime

Quick start:

1. Run `urfu-mcp init` (or `urfu-mcp serve`; first startup also creates the config).
2. Set `sidecar.api_key` to match the local SfeduSchedule sidecar configuration. `config.example.yaml` documents every setting and its default.
3. Run `urfu-mcp auth`. It prompts only for missing `auth.issuer` and `auth.client_id`, validates and securely saves those public OIDC settings, then immediately opens the browser sign-in. You must supply the issuer and client ID registered for your provider/client; do not guess them. The `urfu-mcp auth oidc` / `urfu-mcp oidc` forms remain available.
4. Run `urfu-mcp serve` as an MCP stdio server. The local SfeduSchedule sidecar must already be running at `sidecar.base_url`.

The generated `config.yaml` contains server settings and defaults only. OIDC tokens and the authenticated `person_id` are saved to the operating-system keyring; neither credentials nor person IDs are manually entered into the config. The configured `auth.person_id_claim` is read from the validated ID token during authentication. If the provider does not return a valid UUID in that claim, sign-in fails rather than assigning an identity manually. The YAML file is created with owner-only permissions and is ignored by Git. Its sidecar API key is a random local service credential, not the student's password or Modeus token.

Set `auth.token_kind` explicitly to `id_token` or `access_token` only after confirming the token expected by the target API. It defaults to `null`; running without an explicit choice fails closed instead of selecting a bearer token implicitly.

All runtime settings live in YAML, not environment variables. `urfu-mcp auth credentials` remains an explicit legacy command for storing email/password in the keyring; it is not used by the OIDC login or schedule runtime. The MCP server exposes exactly `retrieve_user_schedule` and `retrieve_person_schedule`. A foreign person can be selected only after the complete search resolves the selection; the default policy allows that explicitly selected match.

The runtime and requests are tested with synthetic tokens and an in-memory HTTP transport. No live URFU/Modeus/OIDC compatibility is claimed. In particular, the actual registered issuer/client and which token kind Modeus accepts must be confirmed for the deployment.
