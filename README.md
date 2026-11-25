# ZCAD_MCP_PYTHON

Локальный **MCP-сервер на Python** (официальный SDK `mcp`, API `MCPServer` v2, транспорт **stdio**) для ZCAD:

1. **Оркестратор скиллов** — ИИ создаёт, хранит и запускает Python-скиллы анализа DXF (`skills/*.py`).
2. **Канал команд к ZCAD Bridge** (HTTPSERVER ZCAD, эндпоинт `/ipc`) — выгрузка чертежа в DXF и отрисовка результата.
3. **Сырая геометрия не уходит в модель**: чертёж лежит в DXF-файле, скилл обрабатывает его в отдельном процессе и
   возвращает только сжатый JSON (`stats` + не более `max_segments` кандидатов).

```text
ZCAD  →  DXF (файл)  →  Python-скилл  →  compact JSON  →  ИИ
ИИ    →  JSON-команда  →  MCP  →  ZCAD Bridge  →  отрисовка
```

## Архитектура

```text
MCP Client (Cursor / Claude / …)
        ↕ stdio (MCP, JSON-RPC)
server.py  (MCPServer "zcad-mcp")
  ├─ skills tools  → skill_runner.py → python -I skill_worker.py (отдельный процесс на запуск)
  ├─ zcad tools    → zcad_client.py  → HTTP POST {ZCAD_BRIDGE_URL}/ipc
  ├─ workspace/    → selection.dxf, .current_dxf.json, артефакты скиллов
  └─ skills/       → *.py анализаторы (поставляемые и созданные ИИ)
```

| Модуль | Назначение |
|--------|------------|
| `server.py` | Точка входа, регистрация tools, `MCPServer`, `run("stdio")` |
| `config.py` | Настройки из env и `.env`, логирование в stderr |
| `zcad_client.py` | HTTP-клиент к ZCAD Bridge (`/ipc`), маппинг tools → команды Bridge |
| `skill_runner.py` | Валидация (AST), хранение, запуск скиллов, обрезка ответа |
| `skill_worker.py` | Процесс-исполнитель скилла с ограничениями (audit hook, лимит памяти) |
| `skill_template.py` | Документированный контракт скилла + рабочий пример |
| `dxf_workspace.py` | Workspace: текущий DXF, резолв путей, bbox из заголовка DXF |
| `skills/` | `extract_line_stats.py`, `find_parallel_lines.py` (поставляются с сервером) |
| `examples/` | Mock ZCAD Bridge, пример скилла `detect_walls`, сквозной сценарий по stdio |
| `tests/` | pytest: mock Bridge, runner, create → run → delete, обрезка, stdio |

## Установка

Требуется Python 3.10+.

```bash
git clone https://github.com/veb86/ZCAD_MCP_PYTHON.git
cd ZCAD_MCP_PYTHON
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt # mcp[cli], ezdxf, httpx, numpy (numpy — опционально)
cp .env.example .env            # при необходимости поправить
python server.py                # сервер ждёт MCP-клиента на stdin/stdout, логи — в stderr
```

При старте сервер создаёт каталоги `workspace/` и `skills/`, если их нет.

### Переменные окружения

Настройки берутся из переменных окружения; дополнительно читается `.env` рядом с `server.py`
(или файл из `ZCAD_MCP_ENV_FILE`). Переменные процесса (например, из конфига MCP-клиента) имеют приоритет над `.env`.
Относительные пути считаются от каталога проекта.

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `ZCAD_BRIDGE_URL` | Базовый URL HTTPSERVER ZCAD (запросы идут на `<url>/ipc`) | `http://127.0.0.1:5000` |
| `ZCAD_MCP_WORKSPACE` | Каталог workspace | `./workspace` |
| `ZCAD_MCP_SKILLS` | Каталог скиллов | `./skills` |
| `ZCAD_MCP_MAX_SEGMENTS` | Макс. элементов в каждом списке ответа скилла | `200` |
| `ZCAD_MCP_SKILL_TIMEOUT_SEC` | Таймаут выполнения скилла, с | `60` |
| `ZCAD_MCP_MAX_RESULT_BYTES` | Макс. размер JSON-ответа скилла | `200000` |
| `ZCAD_MCP_SKILL_MEMORY_MB` | Лимит памяти процесса скилла (POSIX, `0` — без лимита) | `2048` |
| `ZCAD_MCP_MAX_DRAW_LINES` | Макс. сегментов в одном вызове `zcad_draw_*` | `10000` |
| `ZCAD_BRIDGE_TOKEN` | Токен, если Bridge его требует (поле `token` в запросе) | — |
| `ZCAD_BRIDGE_TIMEOUT_SEC` | Таймаут HTTP-запроса к Bridge, с | `35` |
| `ZCAD_EXPORT_COMMAND` | Команда Bridge для выгрузки в DXF | `EXPORT` |
| `ZCAD_EXPORT_WAIT_SEC` | Сколько ждать появления DXF после команды, с | `30` |
| `ZCAD_DRAW_MODE` | `batch` (`BEGIN_BATCH`/`BATCH_LINES`/`END_BATCH`) или `line` (по одной `LINE`) | `batch` |
| `ZCAD_BATCH_CHUNK_SIZE` | Сегментов в одной команде `BATCH_LINES` | `500` |
| `ZCAD_MCP_LOG_LEVEL` | Уровень логов (в stderr) | `INFO` |
| `ZCAD_MCP_ENV_FILE` | Путь к альтернативному `.env` | `./.env` |

## Подключение к MCP-клиенту

Сервер запускается клиентом как дочерний процесс (stdio). Пути — абсолютные; на Windows используйте
`C:\\path\\to\\ZCAD_MCP_PYTHON\\.venv\\Scripts\\python.exe`.

**Claude Desktop** (`claude_desktop_config.json`) и **Cursor** (`.cursor/mcp.json` или `~/.cursor/mcp.json`) —
одинаковый формат:

```json
{
  "mcpServers": {
    "zcad": {
      "command": "/path/to/ZCAD_MCP_PYTHON/.venv/bin/python",
      "args": ["/path/to/ZCAD_MCP_PYTHON/server.py"],
      "env": {
        "ZCAD_BRIDGE_URL": "http://127.0.0.1:5000",
        "ZCAD_MCP_WORKSPACE": "/path/to/ZCAD_MCP_PYTHON/workspace",
        "ZCAD_MCP_SKILLS": "/path/to/ZCAD_MCP_PYTHON/skills",
        "ZCAD_MCP_MAX_SEGMENTS": "200",
        "ZCAD_MCP_SKILL_TIMEOUT_SEC": "60"
      }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add zcad --env ZCAD_BRIDGE_URL=http://127.0.0.1:5000 -- /path/to/.venv/bin/python /path/to/server.py
```

**MCP Inspector** (ручная проверка):

```bash
npx @modelcontextprotocol/inspector python server.py
```

Важно: ZCAD Bridge работает только при запущенном в ZCAD HTTP-сервере (команда `HTTPStart`); его доступность
проверяет tool `zcad_status`. Сервер MCP запускается и без ZCAD — tools скиллов работают с любыми DXF.

## Контракт скилла

Скилл — файл `skills/<name>.py`. Полный контракт с рабочим примером — в [`skill_template.py`](skill_template.py)
(его же возвращает tool `get_skill_template`).

```python
import math

import ezdxf

SKILL = {                                   # литеральный dict (читается без выполнения кода)
    "name": "line_lengths",                 # = имя файла, ^[a-zA-Z_][a-zA-Z0-9_]*$
    "description": "Сумма длин LINE по слоям",
    "params": {"min_length": 0.0},          # значения по умолчанию → JSON-схема в list_skills
}


def run(dxf_path: str, params: dict) -> dict:
    """Читает DXF, анализирует, возвращает ТОЛЬКО сжатый результат."""
    doc = ezdxf.readfile(dxf_path)
    per_layer = {}
    total = 0
    for line in doc.modelspace().query("LINE"):
        total += 1
        s, e = line.dxf.start, line.dxf.end
        length = math.hypot(e.x - s.x, e.y - s.y)
        if length >= params.get("min_length", 0.0):
            per_layer[line.dxf.layer] = per_layer.get(line.dxf.layer, 0.0) + length
    return {
        "ok": True,
        "skill": SKILL["name"],
        "stats": {"lines_total": total, "layers": len(per_layer)},
        "result": {"layers": [{"layer": k, "length": round(v, 3)} for k, v in per_layer.items()]},
        "warnings": [],
    }
```

Правила:

1. `run(dxf_path, params)` — чистая функция чертежа; `params` — значения из `SKILL["params"]`, перекрытые
   `params_json`, плюс всегда `max_segments`.
2. Результат — JSON-сериализуемый `dict` вида `{ok, skill, stats, result, warnings}`
   (если скилл не вернул `ok`/`skill`/`warnings`, runner их добавит).
3. **Нельзя** возвращать полный дамп линий. Для отрисовки — только отобранные кандидаты: сегменты
   `{"x1","y1","x2","y2"}` (для `zcad_draw_lines`) или полилинии `{"points": [[x, y], ...], "closed": bool}`
   (для `zcad_draw_polylines`).
4. **Обрезка (политика):** любой список в ответе длиннее `max_segments` обрезается до `max_segments`, в `warnings`
   добавляется предупреждение, в ключе `truncated` — исходные длины (`{"result.walls": 950}`). Ответ больше `ZCAD_MCP_MAX_RESULT_BYTES`
   отклоняется с `{ok: false, error}`.
5. Скиллы не импортируют друг друга; доступны стандартная библиотека (кроме запрещённых модулей), `ezdxf`, `numpy`.

Поставляемые скиллы:

| Скилл | Что делает |
|-------|-----------|
| `extract_line_stats` | Счётчики сущностей, по слоям, по ориентации, bbox, длины — без списка линий |
| `find_parallel_lines` | Кластеры сегментов по углу (компактные группы) + ограниченный список сегментов самых длинных групп |

## MCP tools

Все tools возвращают JSON-объект с ключом `ok`; при ошибке — `{"ok": false, "error": "...", "error_code"?: "..."}`
(исключения перехватываются, процесс MCP не падает). Исключение — `list_skills`, который возвращает список.

| Tool | Аргументы | Возврат |
|------|-----------|---------|
| `list_skills` | — | `[{name, description, params_schema}]` |
| `get_skill_source` | `name` | `{ok, name, description, source}` |
| `get_skill_template` | — | `{ok, name, source}` — контракт и пример для генерации скилла |
| `create_skill` | `name`, `source_code`, `description=""`, `overwrite=false` | `{ok, name, path, overwritten, warnings}`; если файл есть и `overwrite=false` — ошибка |
| `update_skill` | `name`, `source_code`, `description=""` | как `create_skill(..., overwrite=true)`, но только для существующего скилла |
| `delete_skill` | `name` | `{ok, name, path}` |
| `run_skill` | `name`, `params_json="{}"`, `dxf_path=null`, `max_segments=null` | `{ok, skill, stats, result, warnings, elapsed_sec}`; без `dxf_path` — текущий DXF |
| `get_current_dxf` | — | `{ok, path, exists, size, mtime}` |
| `set_current_dxf` | `path` | `{ok, path, exists, size, mtime}` |
| `zcad_status` | — | `{ok, status, detail?, url}` |
| `zcad_export_selection` | `out_path=null` (по умолчанию `workspace/selection.dxf`) | `{ok, path, size, bbox?, detail?}`; файл становится текущим DXF |
| `zcad_draw_lines` | `lines_json` (`[{x1,y1,x2,y2}]`), `layer="IM_ANALYSIS"`, `color=1` | `{ok, drawn_count, warnings}` |
| `zcad_draw_polylines` | `polylines_json` (`[{points:[[x,y],...], closed?}]`), `layer="IM_WALLS"`, `color=1` | `{ok, drawn_count, segments_drawn, warnings}` |

`create_skill` статически проверяет код (синтаксис, наличие `run(dxf_path, params)`, литеральный `SKILL`,
совпадение имени, запрещённые импорты/вызовы) и **не выполняет** его при сохранении. Запись атомарная
(временный файл + замена).

## Сценарий «стены по выделению»

Пользователь: «Выделил план этажа — определи стены». Агент вызывает:

1. `zcad_status` → `{"ok": true, "status": "ok"}`.
2. `zcad_export_selection` → `{"ok": true, "path": ".../workspace/selection.dxf", "bbox": {...}}` — файл
   становится текущим DXF.
3. `list_skills` / `run_skill("extract_line_stats")` — сводка: сколько линий, какие слои, bbox.
4. Скилла `detect_walls` нет → `get_skill_template`, затем `create_skill("detect_walls", source_code=...)`
   (пример такого скилла — [`examples/detect_walls_skill.py`](examples/detect_walls_skill.py)).
5. `run_skill("detect_walls", params_json='{"min_thickness": 150, "max_thickness": 250}')` — без `dxf_path`,
   используется выгруженный DXF → `{stats: {walls_found: 7, ...}, result: {walls: [{points, closed, thickness, length}]}}`.
6. `zcad_draw_polylines(polylines_json=<result.walls>, layer="IM_WALLS")` → `{"ok": true, "drawn_count": 7}`.

В ответах tools нет перечня всех линий чертежа — только агрегаты и ограниченный набор геометрии для отрисовки.

Сценарий целиком, через настоящий stdio и HTTP-mock ZCAD, запускается одной командой:

```bash
python examples/walls_scenario_demo.py
```

## Ожидаемый API ZCAD Bridge

> **Согласовать с существующим сервером.** Ниже — контракт, под который написан `zcad_client.py`. Он основан
> на текущем HTTPSERVER ZCAD (`/ipc`); всё, что отличается, адаптируется в `zcad_client.py`
> (и/или через `ZCAD_EXPORT_COMMAND`, `ZCAD_DRAW_MODE`) без изменения tools для ИИ.

Транспорт: один эндпоинт `{ZCAD_BRIDGE_URL}/ipc`, JSON, HTTP 200 и для ошибок команд.

| Метод | Тело запроса | Ответ |
|-------|--------------|-------|
| `GET /ipc` | — | `{"status": "ok", "message": "ZCAD HTTP server is available"}` |
| `POST /ipc` | `{"id": "<uuid>", "cmd": "<CMD>", "args": [...], "token"?: "..."}` | `{"id", "status": "ok", "result"?}` или `{"id", "status": "error", "error": "..."}` |

| Tool | Команды Bridge | Статус |
|------|----------------|--------|
| `zcad_status` | `GET /ipc` | есть в ZCAD |
| `zcad_export_selection` | `EXPORT ["<abs path>.dxf"]`, затем ожидание файла (появился, обновился, размер стабилен) | **согласовать**: нужна выгрузка именно выделения; имя команды задаётся `ZCAD_EXPORT_COMMAND` |
| `zcad_draw_lines` | `BEGIN_BATCH []` → `BATCH_LINES [[[x1,y1,x2,y2], ...]]` (порциями по `ZCAD_BATCH_CHUNK_SIZE`) → `END_BATCH []`; или `LINE [x1,y1,x2,y2]` при `ZCAD_DRAW_MODE=line` | есть в ZCAD |
| `zcad_draw_polylines` | полилинии раскладываются на сегменты и рисуются как `zcad_draw_lines` | **согласовать**: `POLYLINE` в ZCAD пока заглушка |

Известные ограничения текущего Bridge (возвращаются в `warnings`, не ломают вызов):

* `LINE` / `BATCH_LINES` не принимают слой и цвет — `layer`/`color` пока не применяются (рисуется на текущем слое).
  Предлагаемое расширение: `BATCH_LINES [[...], {"layer": "IM_WALLS", "color": 1}]` или команда `SET_LAYER`.
* `POLYLINE` / `ARC` — заглушки; полилиния рисуется отдельными отрезками.
* `EXPORT` (если зарегистрирована) сохраняет весь чертёж, а не выделение; путь должен быть доступен процессу ZCAD
  (MCP-сервер и ZCAD на одной машине).
* Bridge ждёт GUI-поток до ~30 с, поэтому `ZCAD_BRIDGE_TIMEOUT_SEC=35`; повторных запросов клиент не делает
  (команды рисования не идемпотентны).
* Ошибки Bridge возвращаются как `{ok: false, error, error_code}`, где `error_code` ∈ `TIMEOUT`, `CONNECTION_ERROR`,
  `HTTP_ERROR`, `INVALID_RESPONSE`, `COMMAND_ERROR`, `EXPORT_TIMEOUT`. Если в пакетном режиме что-то упало,
  `END_BATCH` всё равно отправляется, а в ошибке указано, сколько линий успело уйти.

## Безопасность выполнения скиллов (MVP)

* Запускаются только файлы `skills/<name>.py`; имя — строго `^[a-zA-Z_][a-zA-Z0-9_]*$`.
* Каждый запуск — отдельный процесс `python -I skill_worker.py` с таймаутом `ZCAD_MCP_SKILL_TIMEOUT_SEC`
  (по истечении процесс убивается), лимитом памяти `ZCAD_MCP_SKILL_MEMORY_MB` и окружением без `ZCAD_*`/`PYTHON*`.
* Статическая проверка (при `create_skill` и перед запуском): запрещены `socket`, `http`, `urllib`, `subprocess`,
  `ctypes`, `importlib`, `multiprocessing` и т.п., `eval`/`exec`/`compile`/`__import__`, `os.system`/`os.exec*`/`os.spawn*`,
  относительные импорты.
* Во время выполнения (audit hook): блокируются сеть, создание процессов, `ctypes`, импорт запрещённых модулей,
  запись вне `workspace/`, чтение файлов вне DXF, `workspace/`, `skills/` и каталогов Python/библиотек.
* `print()` скилла уходит в лог сервера (stderr), а не в stdio-канал MCP.
* Ошибки скилла (исключение, таймаут, не-dict, не-JSON) → `{ok: false, error}`; процесс MCP не падает.

Это **не полноценная песочница** — защита от случайных ошибок и типичных опасных вызовов в коде, сгенерированном ИИ.
Жёсткая изоляция (Docker/seccomp/отдельный пользователь) — phase 2.

## Логирование

Логи пишутся **только в stderr** (stdout занят транспортом stdio): старт и конфигурация, каждый вызов tool с
аргументами (имя скилла, путь DXF, размер JSON вместо самих координат), длительность и `ok`, ошибки Bridge
с кодом. Уровень — `ZCAD_MCP_LOG_LEVEL`.

## Тестирование

```bash
pip install -r requirements-dev.txt
pytest                      # ~80 тестов, ZCAD не нужен
```

| Файл | Что проверяет |
|------|---------------|
| `tests/test_zcad_client_mock.py` | Mock ZCAD через `httpx.MockTransport`: `status`, `export` (ожидание файла, таймаут), `draw_lines`/`draw_polylines` (batch и line), ошибки Bridge |
| `tests/test_skill_runner.py` | Поставляемые скиллы на фикстурном DXF (compact keys), create → run → delete, обрезка до `max_segments`, таймаут, исключения, запреты (сеть, `os.system`, запись/чтение вне workspace) |
| `tests/test_server.py` | Tools через in-process MCP-клиент; сценарий «стены по выделению»; current DXF после export |
| `tests/test_stdio.py` | Запуск `server.py` как подпроцесса по stdio: список tools, `run_skill`, `zcad_status` без Bridge |
| `tests/test_config.py` | Env, `.env`, значения по умолчанию, ошибки конфигурации |

Ручная проверка без ZCAD:

```bash
python examples/mock_zcad_bridge.py --port 5000        # терминал 1: mock Bridge
npx @modelcontextprotocol/inspector python server.py   # терминал 2: MCP Inspector (ZCAD_BRIDGE_URL по умолчанию :5000)
```

## Phase 2 (не входит в MVP)

Жёсткая песочница скиллов; пагинация кандидатов; MCP resource `dxf://current/summary`; версионирование скиллов;
Streamable HTTP transport; production-скилл `detect_walls` (проёмы, эвристики толщины); слой/цвет и `POLYLINE` в Bridge.
