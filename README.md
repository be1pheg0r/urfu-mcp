# urfu-mcp

## Единый вход в Modeus и iStudent

`urfu-mcp auth` uses two browser tabs in one explicit Playwright context: Modeus and the fixed iStudent BRS page share a university SSO sign-in. Modeus ID tokens and the separate iStudent session are independently validated; a fresh HTTPS request verifies the protected BRS page before saving the short-lived iStudent session in the OS keyring. The program asks for explicit confirmation that both accounts belong to you. `setup` only reports success when both saved sessions still validate. Modeus tokens are never sent to iStudent.

The iStudent JWT signature is checked against its official realm, and the stored cookie pair is scoped to the exact HTTPS origin. Its lifetime is conservatively capped at 15 minutes because the PHP session lifetime is not known. Windows Credential Manager and WSL keyrings are distinct; authenticate and launch MCP on the same OS. The latest native-Windows `setup` attempt encountered an HTML-marker false negative, now fixed with synthetic tests, but full native-Windows setup and an authenticated BRS MCP call still need an interactive retest. See `.codex/STATE.md`.

## iStudent БРС

`retrieve_brs(subject_name, period)` — третий MCP-инструмент, требует период вида `2026/2027 — Осенний` или `2025/2026 — Весенний`; `subject_name="all"` запрашивает все предметы выбранного периода. Результат включает секции, баллы и веса из защищённых страниц. Запросы идут только с отдельно проверенной iStudent-сессией; при истечении или несовпадении с локальной учётной записью инструмент отказывает в доступе.

Парсер и транспорт проверены синтетическими тестами; защищённая HTML-страница просмотрена с разрешения владельца без сохранения оценок и cookie. Сквозной вызов БРС через запущенный MCP-сервер на Windows пока не подтверждён: не путайте тесты с проверкой живых данных.

## Local Modeus MCP runtime

Запустите из клона репозитория одной командой:

```bash
uv run urfu-mcp setup
```

`uv run` автоматически подготовит синхронизированное окружение проекта по `pyproject.toml` и `uv.lock`, затем откроет wizard. Отдельные `uv sync`, `urfu-mcp init`, `urfu-mcp auth` и `urfu-mcp start` для первого запуска не нужны. Сам `uv` должен быть установлен заранее.

`setup` создаёт `config.yaml`, выполняет общий вход в Modeus и iStudent и проверяет обе сессии. При ошибке возвращает ненулевой код и не запускает MCP-сервер. `--non-interactive` пропускает подтверждение приветствия, `--no-color` отключает цвет; перенаправленный вывод остаётся обычным текстом.

Визард показывает статичный баннер без анимации. При перенаправлении вывода он не добавляет управляющие последовательности терминала.

When setup is complete, add `urfu-mcp` to your MCP client's server configuration and have that client launch `uv run urfu-mcp serve` (or the installed `urfu-mcp serve`). The stdio server must be launched by the MCP host. `urfu-mcp start` remains a foreground stdio alias for an MCP host, not a detached daemon.

Репозиторий пока не публикуется как standalone PyPI-пакет: сначала нужен clone исходного кода и установленный `uv`. Команда `uv run` сама создаёт/синхронизирует окружение из проекта, поэтому отдельный `uv sync --all-groups` не нужен. .NET и SfeduSchedule submodule не требуются.

`urfu-mcp init`, `urfu-mcp auth`, `urfu-mcp start`, `urfu-mcp stop`, `urfu-mcp serve`, `urfu-mcp auth oidc`, and `urfu-mcp auth credentials` remain available as explicit commands.

`auth` посещает Modeus и iStudent в одном Chromium-контексте. Завершите SSO/MFA в браузере при необходимости. Учётные данные приложения можно заранее сохранить в системном keyring; тогда заполнение формы ограничено точным официальным HTTPS-адресом. Команда откажет в успехе, если хотя бы одну из сессий не удалось проверить.

After sign-in, the client reads the Modeus SPA's `oidc.user:*` session entry. It captures trusted Modeus app config or matching OIDC discovery metadata, then validates the ID-token signature, issuer, audience, expiry, and `person_id`. Modeus may provide only an ID token; absent access tokens remain absent. Tokens and identity are stored in the OS keyring. Authentication fails closed if trusted metadata is not observed.

The two schedule operations make bounded, in-process HTTPS requests directly to the fixed `https://urfu.modeus.org` calendar and person-search routes. Each call supplies its selected per-call Bearer token, enforces response-size/time limits, maps upstream failures safely, and requires complete pagination before person selection. There is no general proxy or arbitrary destination setting. Existing `sidecar` config entries from older versions are ignored; `modeus_http` controls request bounds.

`urfu-mcp start` runs the MCP stdio server in the foreground for the launching MCP client. Running it in a standalone terminal does not create a background MCP endpoint. `urfu-mcp stop` from another terminal signals only the recorded MCP process after checking its process creation time. State is private at `~/.urfu-mcp/processes.json`. Windows and WSL process/keyring state are separate; run the MCP client and authentication in the same environment.

`urfu-mcp auth oidc` remains the explicit generic PKCE flow. `urfu-mcp auth credentials` remains available for saving an account to the OS keyring.

Contract and authentication tests use synthetic data. An authenticated native-Windows MCP stdio schedule query was verified against live Modeus on September 24, 2026; person search and an authenticated MCP BRS call are not yet live-verified.
