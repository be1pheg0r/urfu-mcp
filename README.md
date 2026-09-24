# urfu-mcp

## Local Modeus MCP runtime

Запустите из клона репозитория одной командой:

```bash
uv run urfu-mcp setup
```

`uv run` автоматически подготовит синхронизированное окружение проекта по `pyproject.toml` и `uv.lock`, затем откроет wizard. Отдельные `uv sync`, `urfu-mcp init`, `urfu-mcp auth` и `urfu-mcp start` для первого запуска не нужны. Сам `uv` должен быть установлен заранее.

`setup` is the recommended first-run workflow after installation. It checks that the supported Python runtime and dependencies are available, creates or validates `config.yaml` without replacing existing settings, reuses a valid stored Modeus session, and verifies authentication locally. If sign-in is needed, it opens Chromium for you to complete URFU SSO/MFA; authentication remains a human browser step. The wizard does not ask for your password, expose tokens, or launch a standalone MCP server. `--non-interactive` skips the welcome confirmation and `--no-color` disables colors; redirected output is plain and animation-free.

On an interactive color terminal, the **URFU-MCP** greeting rises out of a brief ASCII flame effect. The fire is the existing `asciimatics.renderers.Fire` renderer, with `FigletText` as its heat source, displayed via Rich Live (no alternate screen); the static banner remains afterward. Narrow/short terminals skip the effect. Redirected output, `--no-color`, `NO_COLOR`, and `--non-interactive` immediately use a plain, animation-free banner without terminal control sequences or delays. Ctrl-C restores the cursor and cancels setup. The animation is only part of the `setup` CLI, never MCP stdio output. Source: [asciimatics fire sample](https://github.com/peterbrittain/asciimatics/blob/master/samples/fire.py); upstream [Apache-2.0 license](https://github.com/peterbrittain/asciimatics/blob/master/LICENSE). We use its published renderer API and do not copy the sample code.

When setup is complete, add `urfu-mcp` to your MCP client's server configuration and have that client launch `uv run urfu-mcp serve` (or the installed `urfu-mcp serve`). The stdio server must be launched by the MCP host. `urfu-mcp start` remains a foreground stdio alias for an MCP host, not a detached daemon.

Репозиторий пока не публикуется как standalone PyPI-пакет: сначала нужен clone исходного кода и установленный `uv`. Команда `uv run` сама создаёт/синхронизирует окружение из проекта, поэтому отдельный `uv sync --all-groups` не нужен. .NET и SfeduSchedule submodule не требуются.

`urfu-mcp init`, `urfu-mcp auth`, `urfu-mcp start`, `urfu-mcp stop`, `urfu-mcp serve`, `urfu-mcp auth oidc`, and `urfu-mcp auth credentials` remain available as explicit commands.

`auth` opens Chromium on the Modeus sign-in page. Complete URFU SSO/MFA there. The CLI does not ask for issuer, client ID, token kind, email, or password. If a URFU account is in the `urfu-mcp` OS keyring entry, it can fill the SSO form; otherwise sign in in the browser.

After sign-in, the client reads the Modeus SPA's `oidc.user:*` session entry. It captures trusted Modeus app config or matching OIDC discovery metadata, then validates the ID-token signature, issuer, audience, expiry, and `person_id`. Modeus may provide only an ID token; absent access tokens remain absent. Tokens and identity are stored in the OS keyring. Authentication fails closed if trusted metadata is not observed.

The two schedule operations make bounded, in-process HTTPS requests directly to the fixed `https://urfu.modeus.org` calendar and person-search routes. Each call supplies its selected per-call Bearer token, enforces response-size/time limits, maps upstream failures safely, and requires complete pagination before person selection. There is no general proxy or arbitrary destination setting. Existing `sidecar` config entries from older versions are ignored; `modeus_http` controls request bounds.

`urfu-mcp start` runs the MCP stdio server in the foreground for the launching MCP client. Running it in a standalone terminal does not create a background MCP endpoint. `urfu-mcp stop` from another terminal signals only the recorded MCP process after checking its process creation time. State is private at `~/.urfu-mcp/processes.json`. Windows and WSL process/keyring state are separate; run the MCP client and authentication in the same environment.

`urfu-mcp auth oidc` remains the explicit generic PKCE flow. `urfu-mcp auth credentials` remains available for saving an account to the OS keyring.

Contract and authentication tests use synthetic data. A direct authenticated Modeus schedule query previously returned HTTP 200, but the new Python runtime itself has not been live-integrated against Modeus.
