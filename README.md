# urfu-mcp

MCP-сервер для УрФУ: расписание Modeus и баллы БРС из iStudent. Локальный stdio-сервер, который даёт
MCP-клиенту (Hermes Agent, Claude Code, Codex CLI) доступ к университетским сервисам через один вход в
key.urfu.ru.

[![Python](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-2.x-5c4ee5)](https://modelcontextprotocol.io)
[![version](https://img.shields.io/badge/version-0.1.0-blue)](pyproject.toml)

Проект не публикуется как standalone PyPI-пакет: нужен клон репозитория и установленный
[`uv`](https://docs.astral.sh/uv/). `.NET` и submodule `SfeduSchedule` не требуются.

## Быстрый старт

```bash
git clone https://github.com/be1pheg0r/urfu-mcp.git
cd urfu-mcp
uv run urfu-mcp setup
```

Команда синхронизирует окружение по `uv.lock`, создаёт `config.yaml`, предлагает способ входа (↑/↓ + Enter),
выполняет общий вход в Modeus и iStudent и проверяет обе сохранённые сессии. Отдельные `uv sync`, `init`,
`auth` и `start` для первого запуска не нужны.

```text
urfu-mcp setup [--no-color] [--non-interactive]
```

- `--non-interactive` пропускает подтверждение приветствия; без действующих сессий способ входа выбрать нельзя,
  команда завершится без авторизации.
- `--no-color` отключает цвета, в том числе при перенаправлении вывода.
- Ошибка на любом этапе даёт ненулевой код возврата, сервер не запускается.

## Инструменты

| Инструмент | Что возвращает |
|---|---|
| `retrieve_user_schedule` | расписание авторизованного пользователя на дату или включительный период |
| `retrieve_person_schedule` | расписание явно выбранного человека или нескольких, найденных полным поиском |
| `retrieve_brs` | БРС по точному имени предмета либо все предметы периода |
| `retrieve_courses_list` | список enrolled-курсов elearn.urfu.ru: `course_id`, название, группа прогресса |
| `retrieve_course_content` | разделы и элементы одного курса elearn по `course_id` или точному названию |

`retrieve_brs(subject_name, period)` требует явный период — `2026/2027 — Осенний` или `2025/2026 — Весенний`;
проверка периода идёт до обращения к идентичности и сессии. `subject_name="all"` возвращает все предметы
периода. В ответе секции, баллы и веса; при истечении сессии или несовпадении личности инструмент
отказывает в доступе.

`retrieve_course_content(course)` принимает либо числовой `course_id` из `retrieve_courses_list`, либо
точное название курса. Селектор всегда разрешается только через подтверждённый список курсов: произвольный
`id` не открывается, неоднозначное название отказывает, а страница курса обязана подтвердить тот же `id`.
Названия разделов, типы элементов (`modtype`) и ссылки возвращаются как есть; оценки и сроки выполнения
не выводятся, так как на странице они не подтверждены источником.

## Регистрация в MCP-клиентах

```bash
bash scripts/install-mcp.sh                        # все найденные клиенты
bash scripts/install-mcp.sh --client codex         # один клиент
bash scripts/install-mcp.sh --client hermes --dry-run
bash scripts/install-mcp.sh --runtime native       # Linux/macOS вместо Windows uv через cmd.exe
```

| Флаг | Значения |
|---|---|
| `--client` | `all` · `hermes` · `codex` · `claude` |
| `--runtime` | `auto` · `windows` · `native` |
| `--name` | имя MCP-сервера у клиента, по умолчанию `urfu-mcp` |
| `--dry-run` | печатает регистрации, не меняя конфигурацию агента |

Скрипт вызывает штатные `mcp add` клиентов и регистрирует stdio-запуск из текущего клона. Фоновый сервер
не запускается, пароль в конфигурацию агента не попадает.

Регистрация вручную: добавьте `urfu-mcp` в конфигурацию клиента так, чтобы хост запускал
`uv run urfu-mcp serve`. `urfu-mcp start` — foreground-алиас stdio для MCP-хоста, а не демон.

> Windows Credential Manager и WSL keyring — разные хранилища. Выполняйте `uv run urfu-mcp setup` и запуск
> MCP-сервера в одной ОС. Режим Windows требует пути к клону без пробелов и спецсимволов командной строки.

### Команды CLI

| Команда | Назначение |
|---|---|
| `setup` | конфигурация, вход, проверка обеих сессий |
| `init` | создать `config.yaml` с безопасными значениями |
| `auth` | выбор способа входа: браузерный или сохранённые учётные данные |
| `auth login-saved` | сохранить учётные данные в keyring и войти через официальный SSO |
| `auth credentials` | сохранить почту и пароль отдельно |
| `auth oidc` | явный generic PKCE-флоу по настройкам из `config.yaml` |
| `elearn` | отдельный вход в elearn.urfu.ru с сохранением проверенной Moodle-сессии |
| `elearn login-saved` | тот же вход с учётными данными из системного keyring |
| `serve` | MCP stdio-сервер |
| `start` | foreground-алиас stdio для MCP-хоста |
| `stop` | сигнал записанному PID после проверки времени создания процесса |

Состояние процессов приватно и лежит в `~/.urfu-mcp/processes.json`; у Windows и WSL оно независимое.

## Вход

`urfu-mcp auth` предлагает два способа:

1. **Браузерный** — приложение не читает пароль из хранилища, вход выполняет пользователь.
2. **Сохранённые учётные данные** — запись из системного keyring вводится только на проверенной официальной
   HTTPS-форме key.urfu.ru; если записи нет, почта и пароль запрашиваются один раз локально.

Оба варианта используют один Chromium-контекст и отказывают в успехе, если хотя бы одну сессию не удалось
проверить. Неподдерживаемая форма, повторный запрос пароля после автоматического ввода или недоступное
хранилище приводят к отказу, а не к переключению на ручной ввод. MFA со стороны сервера может потребовать
участия пользователя. Это официальный браузерный SSO, не password grant. При перенаправлении ввода-вывода
интерактивный выбор не запускается.

После входа клиент читает запись `oidc.user:*` из SPA Modeus, берёт доверенный app-конфиг (встроенный JWKS)
или OIDC discovery-метаданные, сверяет client ID сессии и валидирует подпись ID-токена, issuer, audience,
срок и `person_id`. Отсутствующий access-токен не выдумывается. Если доверенные метаданные не наблюдались —
аутентификация fail-closed.

## Конфигурация

`config.yaml` создаётся в корне проекта с правами owner-only, пишется атомарно и игнорируется git.
Все поля с комментариями — в [`config.example.yaml`](config.example.yaml). Секреты и `person_id` в конфиг
не записываются; настройки читаются из YAML, а не из переменных окружения. Ключи `sidecar` из старых версий
игнорируются — API-ключ и base URL sidecar не читаются.

```yaml
auth:
  token_kind: null            # id_token | access_token — заполняется после первого входа

modeus_http:                  # границы прямых HTTPS-запросов к фиксированному host Modeus
  timeout_seconds: 10.0
  max_response_bytes: 2000000
  event_page_size: 1000
  person_page_size: 50
  person_max_pages: 10

schedule:
  timezone: Asia/Yekaterinburg
  max_days: 14
  max_subjects: 3

elearn:
  files_directory: null       # по умолчанию ~/.urfu-mcp/files; явный каталог можно задать здесь
```

`retrieve_course_file` сохраняет только перечисленные для курса файлы в этот приватный каталог; пути внутри
Git working tree блокируются, даже если они указаны в `elearn.files_directory`.

## Безопасность

- Токены Modeus и сессия iStudent хранятся только в OS keyring; конфиг секретов не содержит.
- Токены Modeus не отправляются в iStudent.
- ID-токен проверяется по подписи, issuer, audience, expiry и `person_id`; JWT iStudent — по официальному
  realm JWKS.
- Пара cookie привязана к точному HTTPS-origin, редиректы запрещены, страница проверяется структурно.
- Сессия iStudent ограничена 15 минутами: время жизни PHP-сессии неизвестно.
- Запросы идут только к фиксированным маршрутам `urfu.modeus.org` и защищённой странице БРС; общего прокси
  и произвольных адресов нет.
- Сессия iStudent пишется в keyring ограниченными чанками с generation-манифестом и проверкой целостности.
- Неподдерживаемый `token_kind` или отсутствие доверенных метаданных даёт `NotAuthenticated` без обращения
  к keyring.

Парсер и транспорт проверены синтетическими тестами; защищённые страницы просматривались с разрешения
владельца без сохранения оценок, имён, идентификаторов и cookie.

## Архитектура

```text
MCP-клиент
   │ stdio
   ▼
urfu-mcp serve ──► runtime ──► identity + token (keyring)
   │                           │
   ├── modeus/mcp_tools.py     └──► HTTPS → urfu.modeus.org (календарь, поиск людей, HAL)
   │     retrieve_user_schedule
   │     retrieve_person_schedule
   │
   └── istudent/mcp_tools.py       └──► HTTPS → istudent.urfu.ru (БРС, origin-scoped cookie)
         retrieve_brs
```

`urfu_mcp/auth/` — Playwright-вход, OIDC/PKCE, keyring. `urfu_mcp/modeus/` — HTTP-клиент, нормализатор HAL,
`ScheduleReader`. `urfu_mcp/istudent/` — парсер БРС, источник защищённых страниц, транспорт.
`urfu_mcp/setup.py` — визард, `config.py` — YAML, `process_manager.py` — учёт PID.

Запросы расписания ограничены по размеру и времени, используют свой Bearer-токен на вызов и требуют полной
пагинации до выбора человека. `ScheduleReader` делает отдельный запрос на каждый UUID, исключает события
вне `[start, end)`, дедуплицирует и сортирует.

| Документ | О чём |
|---|---|
| [`.codex/STATE.md`](.codex/STATE.md) | состояние проекта |
| [`.codex/architecture/unified-auth.md`](.codex/architecture/unified-auth.md) | единый SSO, SAML broker, cookie и токены |
| [`.codex/architecture/modeus-schedule.md`](.codex/architecture/modeus-schedule.md) | режиссёр расписанием и границы запросов |
| [`.codex/architecture/istudent-brs.md`](.codex/architecture/istudent-brs.md) | контракт парсера БРС |
| [`docs/design/cli-first-run-wizard.md`](docs/design/cli-first-run-wizard.md) | дизайн визарда первого запуска |
| [`.codex/CHANGELOG.md`](.codex/CHANGELOG.md) | журнал изменений |
| [`.codex/GITRULES.md`](.codex/GITRULES.md) | правила работы с git |

## Тесты

```bash
uv run pytest
uv run ruff check .
uv run mypy urfu_mcp
```

307 тестов на синтетических HAL-ответах Modeus и синтетических страницах БРС: навигация по периоду,
полнота деталей, иерархия взвешенных баллов, отказ на испорченной или истёкшей сессии, границы origin и
пути, roundtrip keyring при ограниченном бэкенде и частичной записи.

## Состояние

Ветка `feat/elearn-engine`, версия `0.1.0`.

| | |
|---|---|
| Проверено | Живой запрос расписания Modeus через MCP stdio на native Windows, 24.09.2026 |
| Проверено | Windows MCP stdio handshake перечисляет все три инструмента |
| Проверено | Авторизованный осмотр защищённой страницы БРС: 4 предмета 2026/2027 осенний, 10 за 2025/2026 весенний, `raw × вес = взвешенный балл` |
| Не проверено | Интерактивный `setup` на native Windows и авторизованный вызов БРС через MCP |
| Не проверено | Живая совместимость person search — только синтетика |
| Не изучено | Время жизни и обновление серверной PHP-сессии iStudent |

Синтетические тесты не равны проверке живых данных.

## Требования

Python 3.12+, `uv`, Chromium (Playwright ставит его при первом входе). Windows, WSL, Linux, macOS — вход
и запуск MCP-сервера должны выполняться в одной ОС.

## Разработка

[`AGENTS.md`](AGENTS.md) — инструкции для агентов. Тесты обязательны и не требуют доступа к университетским
сервисам.
