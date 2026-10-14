# Task Manager

Менеджер фоновых задач: HTTP-запрос ставит задачу в очередь в БД, отдельный
процесс-контроллер забирает её и выполняет строго по одной.

Документ фиксирует архитектуру и интерфейсы до начала реализации кода.
Это гипотеза для эксперимента — намеренно не углубляемся в логирование,
ретраи и конкурентность.

---

## 1. Идея

Три независимых слоя:

1. **API (FastAPI)** — принимает HTTP, превращает запрос в запись в БД.
2. **Очередь (БД)** — durable-буфер задач между API и исполнителем.
3. **Контроллер (отдельный процесс)** — читает очередь, выполняет задачи,
   пишет статусы.

Разделение даёт: API мгновенно отвечает, выполнение переживает рестарт API,
задачи можно выполнять строго по одной.

---

## 2. Сущности

Важно разделять «задачу как код» и «задачу как запись в БД».

| Понятие | Что это | Где живёт |
|---|---|---|
| **Задача (код)** | Класс с логикой, зарегистрированный по имени | `tasks/`, мапа `TASKS` |
| **Задача (БД)** | Факт постановки в очередь: `name` + `params` | `task_manager.task` |
| **Очередь** | Ссылка на задачу + порядок выполнения | `task_manager.task_queue` |
| **Статус** | Текущее состояние задачи | `task_manager.task_status` |

Задача-код не хранится в БД. В БД хранится то, что уже поставили в очередь
(`Task` + `TaskQueue`) и то, что сейчас выполняется (`TaskStatus`).

---

## 3. Поток выполнения

```
HTTP POST /fibonacci            (ручка на конкретный отчёт)
      │  left, right
      ▼
┌─────────────────────────────┐
│ Handler (процесс FastAPI)   │
│  enqueue("fibonacci",       │
│          {left, right})     │
│                             │
│  get_task(name)             │
│  validate_params(params)    │
│  INSERT Task                │
│  INSERT TaskQueue           │
│  INSERT TaskStatus          │
│    status = pending         │
│  (одна транзакция)          │
│  → ответ {"task_id": ...}   │
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│ Controller (отдельный proc) │
│  цикл поллинга:             │
│   • есть задача in_progress?│──да──► ждём
│   • нет: взять pending с    │
│     минимальным order       │
│   • поставить in_progress   │
│   • spawn дочернего процесса│
│     с (task_id, name, params)│
│   • ждать exit code         │
│   • success  либо           │
│     failed (+ error)        │
└─────────────────────────────┘
```

Правило: **одновременно выполняется только одна задача**. Если в очереди
уже есть выполняющаяся — новая ждёт своей очереди.

---

## 4. Жизненный цикл статуса

```
pending ──► in_progress ──► success
                       └──► failed
```

| Статус | Когда ставится | Кто ставит |
|---|---|---|
| `pending` | при постановке в очередь | Launcher |
| `in_progress` | перед запуском дочернего процесса | Controller |
| `success` | процесс завершился с кодом 0 | Controller |
| `failed` | процесс завершился с ошибкой / исключением | Controller |

На текущем этапе `task_status` — **одна строка на задачу**, поле `status`
обновляется при переходах. Позже заменим на append-only лог с временем
исполнения и прочей диагностикой.

---

## 5. Интерфейсы

### 5.1. Базовая задача — `tasks/base.py`

```python
from abc import ABC, abstractmethod


class BaseTask(ABC):
    def __init__(self, task_id: int, params: dict):
        self.task_id = task_id
        self.params = params

    @classmethod
    def validate_params(cls, raw: dict) -> dict:
        # по умолчанию — без валидации; конкретные задачи переопределяют
        return raw

    @abstractmethod
    async def run(self) -> None:
        ...
```

`run()` — async, чтобы задачи при необходимости могли работать с БД через
`asyncpg`/сессии. Чистый CPU-код (polars) внутри async-функции допустим:
задача изолирована в своём процессе.

`task_id` прокидывается в задачу, чтобы она могла использовать его для
именования результатов (например, `reports/task_{task_id}_{time()}.xlsx`).

### 5.2. Реестр задач — `tasks/registry.py`

```python
TASKS: dict[str, type[BaseTask]] = {}


def register(name: str):
    def wrap(cls: type[BaseTask]) -> type[BaseTask]:
        TASKS[name] = cls
        return cls
    return wrap


def get_task(name: str) -> type[BaseTask]:
    # KeyError, если задачи с таким именем нет
    return TASKS[name]
```

Реестр — обычная мапа `имя -> класс`. Наполняется импортом модулей задач
(см. `tasks/__init__.py`). Контроллер и launcher достают класс из мапы по имени.

### 5.3. Постановка в очередь — `launcher.py`

```python
async def enqueue(name: str, params: dict) -> int:
    """Создаёт Task + TaskQueue + TaskStatus(pending) в одной транзакции.

    Возвращает id созданной задачи.
    """
    ...
```

Работает в процессе FastAPI. Сначала берёт класс из реестра
(`get_task(name)`), чтобы отсеять неизвестные имена до записи в БД,
затем валидирует `params`.

### 5.4. Контроллер — `worker/controller.py`

```python
def controller_process_main() -> None:
    """Точка входа отдельного процесса. Запускается через spawn."""
    ...


async def controller_loop() -> None:
    """Поллит очередь, выполняет по одной задаче."""
    ...
```

### 5.5. Дочерний процесс задачи — `worker/runner.py`

```python
def run_task_process(task_id: int, name: str, params: dict) -> None:
    """Target для multiprocessing.Process (spawn).

    Резолвит класс из реестра, инстанцирует BaseTask(task_id, params)
    и запускает run(). Ненулевой exit code сигнализирует контроллеру об ошибке.
    """
    ...
```

---

## 6. HTTP API

На каждую задачу — своя ручка (в реальном кейсе тоже: отдельная ручка на
каждый отчёт). Постановочная ручка валидирует вход, вызывает `enqueue(...)`
и сразу возвращает id задачи; выполнение идёт в фоне.

### POST /fibonacci

Запускает отчёт по числам Фибоначчи.

Запрос:

```json
{ "left": 1, "right": 35 }
```

Ответ (202):

```json
{ "task_id": 1234567890 }
```

### GET /tasks/{task_id}

Статус одной задачи.

Ответ (200):

```json
{
  "task_id": 1234567890,
  "name": "fibonacci",
  "params": { "left": 1, "right": 35 },
  "status": "in_progress",
  "error": null
}
```

404, если задачи нет.

### GET /tasks

Список задач (свежие первыми), опционально с фильтром по статусу:
`GET /tasks?status=success`.

Ответ (200):

```json
{
  "tasks": [
    {
      "task_id": 1234567890,
      "name": "fibonacci",
      "status": "success",
      "error": null
    }
  ]
}
```

Ручки `/tasks` — общие для всех задач (мониторинг), а `/fibonacci` — постановщик
конкретного отчёта. По мере добавления задач появляются новые постановочные
ручки (`/sales_report`, `/users_export`, ...), но `/tasks` остаётся общим.

---

## 7. Пример задачи — `tasks/fibonacci.py`

```python
from pathlib import Path
from time import time

import polars as pl

from tasks.base import BaseTask
from tasks.registry import register

REPORTS_DIR = Path("reports")


@register("fibonacci")
class FibonacciTask(BaseTask):
    async def run(self) -> None:
        left = self.params["left"]
        right = self.params["right"]

        a, b = 0, 1
        sequence = []
        for _ in range(right):
            a, b = b, a + b
            sequence.append(a)

        fibs = sequence[left - 1:right]  # числа с номерами left..right

        REPORTS_DIR.mkdir(exist_ok=True)
        output = REPORTS_DIR / f"task_{self.task_id}_{int(time())}.xlsx"

        df = pl.DataFrame({"n": list(range(left, right + 1)), "fib": fibs})
        df.write_excel(output)
```

Задача вычисляет числа Фибоначчи с номерами от `left` до `right` включительно
и пишет их в Excel через polars `DataFrame`. Параметры приходят прямо из API:

| Параметр | Тип | Описание |
|---|---|---|
| `left` | int | первый номер последовательности |
| `right` | int | последний номер последовательности |

Пример запроса: `{"name": "fibonacci", "params": {"left": 1, "right": 35}}`.

Отчёт сохраняется на диск в директорию `reports/` проекта с именем
`task_{task_id}_{time()}.xlsx`, где `task_id` — id записи задачи в БД,
`time()` — unix-timestamp момента выполнения.

---

## 8. Структура файлов

```
main.py                  # FastAPI: lifespan (spawn контроллера), ручки API
db.py                    # engine + get_async_session (без изменений)
models.py                # правки моделей
launcher.py              # enqueue()
tasks/
  __init__.py            # импорт модулей задач (наполняет реестр)
  base.py                # BaseTask
  registry.py            # TASKS, register(), get_task()
  fibonacci.py           # пример задачи
worker/
  __init__.py
  controller.py          # controller_process_main + controller_loop
  runner.py              # run_task_process (target дочернего процесса)
reports/                 # сюда пишутся отчёты: task_{id}_{time()}.xlsx
alembic/versions/...     # новая миграция под правки схемы
```

---

## 9. Изменения схемы БД

Текущие модели: `Task`, `TaskQueue`, `TaskStatus`.

Предлагаемые правки (одной миграцией):

- `task_status`: добавить `error` (nullable text) — текст ошибки для `failed`.
- `task_queue.order`: убрать `autoincrement=True`. В PostgreSQL автоинкремент
  на не-первичном ключе не превращается в sequence, поле не заполняется.
  Вместо него для FIFO используем `task_queue.id` (TSID — монотонный по
  времени, годится как порядок постановки).
- В будущем: append-only статус-лог с `started_at` / `finished_at` / результатом.
  Сейчас не делаем.

---

## 10. Технические заметки

- **spawn обязателен.** Дочерние процессы поднимаем через
  `multiprocessing.get_context("spawn")`, чтобы не наследовать event loop
  FastAPI и открытые `asyncpg`-соединения.
- **Кто что делает.** Дочерний процесс выполняет только логику `run()`
  (и при необходимости поднимает собственную сессию через `asyncio.run`).
  Все переходы статусов пишет контроллер — единая точка правды.
- **Запуск контроллера.** Спавнится из `lifespan` FastAPI при старте,
  завершается при остановке приложения.
- **Каверза reload.** `uvicorn --reload` может поднять несколько экземпляров
  приложения и, соответственно, несколько контроллеров. При разработке
  учитывать это (один контроллер на рабочую БД).
- **Поллинг.** Интервал опроса очереди — константа (ориентировочно 1 секунда).
  Позже при необходимости заменим на `LISTEN/NOTIFY` PostgreSQL.

---

## 11. Что дальше

1. Правки `models.py` + alembic-миграция.
2. `tasks/` — base, registry, fibonacci.
3. `launcher.py` — enqueue.
4. `worker/` — controller loop + child runner.
5. `main.py` — lifespan + `POST /fibonacci`, `GET /tasks`, `GET /tasks/{id}`.
6. Ручная проверка: `POST /fibonacci` получаем `task_id`, затем запрашиваем
   `GET /tasks/{task_id}` и наблюдаем переход `pending → in_progress → success`,
   появляется файл `reports/task_{id}_{time()}.xlsx`.
