# urfu-mcp

## Единый вход в Modeus и iStudent

`urfu-mcp auth` uses two browser tabs in one explicit Playwright context: Modeus and the fixed iStudent BRS page share a university SSO sign-in. Modeus ID tokens and the separate iStudent session are independently validated; a fresh HTTPS request verifies the protected BRS page before saving the short-lived iStudent session in the OS keyring. `setup` only reports success when both saved sessions still validate. Modeus tokens are never sent to iStudent.

The iStudent JWT signature is checked against its official realm, and the stored cookie pair is scoped to the exact HTTPS origin. Its lifetime is conservatively capped at 15 minutes because the PHP session lifetime is not known. Windows Credential Manager and WSL keyrings are distinct; authenticate and launch MCP on the same OS. The latest native-Windows `setup` attempt reached session storage and then failed; bounded keyring storage and its Windows contract tests have since been fixed, but full interactive setup and an authenticated BRS MCP call still need a retest. See `.codex/STATE.md`.

## iStudent БРС

`retrieve_brs(subject_name, period)` — третий MCP-инструмент, требует период вида `2026/2027 — Осенний` или `2025/2026 — Весенний`; `subject_name="all"` запрашивает все предметы выбранного периода. Результат включает секции, баллы и веса из защищённых страниц. Запросы идут только с отдельно проверенной iStudent-сессией; при истечении или несовпадении с локальной учётной записью инструмент отказывает в доступе.

Парсер и транспорт проверены синтетическими тестами; защищённая HTML-страница просмотрена с разрешения владельца без сохранения оценок и cookie. Сквозной вызов БРС через запущенный MCP-сервер на Windows пока не подтверждён: не путайте тесты с проверкой живых данных.

## Local Modeus MCP runtime

Запустите из клона репозитория одной командой:

```bash
uv run urfu-mcp setup
```

`uv run` автоматически подготовит синхронизированное окружение проекта по `pyproject.toml` и `uv.lock`, затем откроет wizard. Отдельные `uv sync`, `urfu-mcp init`, `urfu-mcp auth` и `urfu-mcp start` для первого запуска не нужны. Сам `uv` должен быть установлен заранее.

`setup` создаёт `config.yaml`, при необходимости предлагает способ входа (стрелки ↑/↓ и Enter), выполняет общий вход в Modeus и iStudent и проверяет обе сессии. При ошибке возвращает ненулевой код и не запускает MCP-сервер. `--non-interactive` пропускает подтверждение приветствия, но при отсутствии действующих сессий не может выбрать способ входа и завершится без авторизации; `--no-color` отключает цвет.

Визард показывает статичный баннер без анимации. При перенаправлении вывода он не добавляет управляющие последовательности терминала.

When setup is complete, add `urfu-mcp` to your MCP client's server configuration and have that client launch `uv run urfu-mcp serve` (or the installed `urfu-mcp serve`). The stdio server must be launched by the MCP host. `urfu-mcp start` remains a foreground stdio alias for an MCP host, not a detached daemon.

Репозиторий пока не публикуется как standalone PyPI-пакет: сначала нужен clone исходного кода и установленный `uv`. Команда `uv run` сама создаёт/синхронизирует окружение из проекта, поэтому отдельный `uv sync --all-groups` не нужен. .NET и SfeduSchedule submodule не требуются.

`urfu-mcp auth` открывает интерактивный выбор стрелками ↑/↓ и Enter: OAuth (browser sign-in) или Stashed credentials (automatic SSO). В первом варианте пароль из хранилища не читается, вход выполняется пользователем в браузере. Во втором уже сохранённые данные берутся из системного keyring и вводятся программой только на проверенной официальной HTTPS-форме SSO; если записи нет, почта и пароль запрашиваются один раз локально (пароль скрыт). Повторный вход не требует вводить их в браузере. Неподдерживаемая форма, повторный запрос пароля после первого автоматического ввода или недоступное хранилище приводят к отказу, а не к незаметному переходу на ручной ввод; в таком случае выберите браузерный способ. Требуемое сервером MFA всё ещё может потребовать участия пользователя. Это тот же официальный браузерный SSO, не password grant.

`urfu-mcp auth login-saved` остаётся совместимым прямым запуском второго варианта; `urfu-mcp auth credentials` сохраняет данные отдельно. `urfu-mcp init`, `urfu-mcp start`, `urfu-mcp stop`, `urfu-mcp serve` и `urfu-mcp auth oidc` остаются доступными. При перенаправлении ввода/вывода интерактивный выбор не запускается.

Оба варианта посещают Modeus и iStudent в одном Chromium-контексте и откажут в успехе, если хотя бы одну из сессий не удалось проверить. Автоматический ввод при сохранённых данных проверен только синтетическими тестами: для подтверждения реального Windows-входа нужен повторный интерактивный запуск пользователя.

After sign-in, the client reads the Modeus SPA's `oidc.user:*` session entry. It captures trusted Modeus app config or matching OIDC discovery metadata, then validates the ID-token signature, issuer, audience, expiry, and `person_id`. Modeus may provide only an ID token; absent access tokens remain absent. Tokens and identity are stored in the OS keyring. Authentication fails closed if trusted metadata is not observed.

The two schedule operations make bounded, in-process HTTPS requests directly to the fixed `https://urfu.modeus.org` calendar and person-search routes. Each call supplies its selected per-call Bearer token, enforces response-size/time limits, maps upstream failures safely, and requires complete pagination before person selection. There is no general proxy or arbitrary destination setting. Existing `sidecar` config entries from older versions are ignored; `modeus_http` controls request bounds.

`urfu-mcp start` runs the MCP stdio server in the foreground for the launching MCP client. Running it in a standalone terminal does not create a background MCP endpoint. `urfu-mcp stop` from another terminal signals only the recorded MCP process after checking its process creation time. State is private at `~/.urfu-mcp/processes.json`. Windows and WSL process/keyring state are separate; run the MCP client and authentication in the same environment.

`urfu-mcp auth oidc` remains the explicit generic PKCE flow. `urfu-mcp auth credentials` remains available for saving an account to the OS keyring.

Contract and authentication tests use synthetic data. An authenticated native-Windows MCP stdio schedule query was verified against live Modeus on September 24, 2026; person search and an authenticated MCP BRS call are not yet live-verified.
