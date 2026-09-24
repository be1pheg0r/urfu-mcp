# urfu-mcp

## Unified auth status (fail-closed)

`urfu-mcp auth` opens **one explicit Playwright context** and visits Modeus followed by the fixed iStudent BRS tile in the same page. URFU SSO may reuse the interactive login; MFA or cancellation still requires user action. Modeus SPA ID tokens are validated against trusted metadata and saved only to the OS keyring. **The command currently exits nonzero even if the iStudent page loads**: protected-page navigation alone does not prove a safely reusable iStudent session. No iStudent cookie/token is copied to keyring, Modeus tokens are never passed to iStudent, and BRS runtime remains unavailable. `setup` likewise cannot report ready with only a Modeus token. Do not run a second `auth` expecting to repair the missing contract.

In an authorized browser on September 24, 2026, iStudent redirected through the official `keys.urfu.ru` OIDC `istudent` code/PKCE client and `/student/keycloak-login` callback. The browser held host-scoped session cookies `keycloakAccessToken` and `PHPSESSID`, without persistent cookie expiry; neither had the Secure attribute. The first contained JWT-shaped claims including `azp=istudent` and `exp`, but no `aud`. The iStudent person GUID did **not** match Modeus's `person_id`; other sampled identifier fields did not match either. These observations are not a validated signature, cross-client identity mapping, server-side PHP-session lifetime or renewal guarantee. Until those are established in a consented native auth flow, iStudent session persistence and an authenticated source must stay disabled. Windows Credential Manager and WSL keyrings are distinct: this WSL environment has no viable secure keyring backend, and no Windows end-to-end sign-in was performed. See `.codex/architecture/unified-auth.md`.

## iStudent БРС: граница подтверждения

`retrieve_brs(subject_name)` зарегистрирован, но пока возвращает безопасную ошибку недоступности. В собственной авторизованной браузерной сессии 24 сентября 2026 подтверждены защищённый HTML-список БРС, поля выбора группы/учебного года/семестра и колонки «Дисциплина», «Итоговый балл», «Итоговая оценка». Добавлен **отключённый от runtime** HTML-парсер проверенной числовой формы балла с обезличенными синтетическими тестами. Браузер по умолчанию показывал прошлый весенний семестр; для текущего осеннего понадобилось выбрать другой учебный год. Парсер сам период по дате не выбирает.

Отдельный SSO-клиент iStudent использует `keys.urfu.ru` (authorization code + PKCE), но автономный жизненный цикл его сессии — получение, привязка к пользователю, продление/истечение и cookie scope — **не подтверждён**. Нельзя переносить cookie из браузера в сервер или подставлять токен Modeus. Поэтому production source и MCP wiring не включены, живой вызов БРС через MCP не выполнялся. Следующий шаг: исследовать легитимный самостоятельный вход iStudent и правила выбора текущего семестра, затем проверить parser/source на своём аккаунте в памяти и выполнить read-only MCP stdio вызов. HTML, персональные предметы/баллы и значения секретов не сохранялись.

## Local Modeus MCP runtime

Запустите из клона репозитория одной командой:

```bash
uv run urfu-mcp setup
```

`uv run` автоматически подготовит синхронизированное окружение проекта по `pyproject.toml` и `uv.lock`, затем откроет wizard. Отдельные `uv sync`, `urfu-mcp init`, `urfu-mcp auth` и `urfu-mcp start` для первого запуска не нужны. Сам `uv` должен быть установлен заранее.

`setup` is the intended first-run workflow, but currently **cannot complete** because a safe iStudent session contract has not been established. It may initialize `config.yaml` and authenticate Modeus, then returns a nonzero status rather than claiming both clients are ready. It does not launch the MCP server. `--non-interactive` skips the welcome confirmation and `--no-color` disables colors; redirected output is plain.

When setup is complete, add `urfu-mcp` to your MCP client's server configuration and have that client launch `uv run urfu-mcp serve` (or the installed `urfu-mcp serve`). The stdio server must be launched by the MCP host. `urfu-mcp start` remains a foreground stdio alias for an MCP host, not a detached daemon.

Репозиторий пока не публикуется как standalone PyPI-пакет: сначала нужен clone исходного кода и установленный `uv`. Команда `uv run` сама создаёт/синхронизирует окружение из проекта, поэтому отдельный `uv sync --all-groups` не нужен. .NET и SfeduSchedule submodule не требуются.

`urfu-mcp init`, `urfu-mcp auth`, `urfu-mcp start`, `urfu-mcp stop`, `urfu-mcp serve`, `urfu-mcp auth oidc`, and `urfu-mcp auth credentials` remain available as explicit commands.

`auth` visits Modeus and then iStudent in the same Chromium context. Complete URFU SSO/MFA when needed. It does not ask for issuer/client ID or password in the CLI. An explicitly saved application credential may fill the exact HTTPS ADFS login form; otherwise sign in manually. The current command returns nonzero after the visit because the iStudent session contract is not verified; only validated Modeus tokens may be stored.

After sign-in, the client reads the Modeus SPA's `oidc.user:*` session entry. It captures trusted Modeus app config or matching OIDC discovery metadata, then validates the ID-token signature, issuer, audience, expiry, and `person_id`. Modeus may provide only an ID token; absent access tokens remain absent. Tokens and identity are stored in the OS keyring. Authentication fails closed if trusted metadata is not observed.

The two schedule operations make bounded, in-process HTTPS requests directly to the fixed `https://urfu.modeus.org` calendar and person-search routes. Each call supplies its selected per-call Bearer token, enforces response-size/time limits, maps upstream failures safely, and requires complete pagination before person selection. There is no general proxy or arbitrary destination setting. Existing `sidecar` config entries from older versions are ignored; `modeus_http` controls request bounds.

`urfu-mcp start` runs the MCP stdio server in the foreground for the launching MCP client. Running it in a standalone terminal does not create a background MCP endpoint. `urfu-mcp stop` from another terminal signals only the recorded MCP process after checking its process creation time. State is private at `~/.urfu-mcp/processes.json`. Windows and WSL process/keyring state are separate; run the MCP client and authentication in the same environment.

`urfu-mcp auth oidc` remains the explicit generic PKCE flow. `urfu-mcp auth credentials` remains available for saving an account to the OS keyring.

Contract and authentication tests use synthetic data. A direct authenticated Modeus schedule query previously returned HTTP 200, but the new Python runtime itself has not been live-integrated against Modeus.
