# SOLUTION.md — устройство сервиса Task Manager (для агента-разработчика)

Этот документ описывает **полностью текущее работающее решение**: что оно
делает, из каких частей состоит, какие решения приняты и **почему именно так**.
Ты (агент) должен на его основе сделать **новую задачу в рамках уже работающего
сервиса**, не ломая существующую архитектуру.

Читай документ целиком перед тем, как писать код. Раздел 13 — практический
чек-лист «как добавить новую задачу».

---

## 1. Что это и зачем

Сервис обрабатывает **фоновые задачи** (в перспективе — построение отчётов из
данных через polars/Excel). Схема:

```
HTTP-запрос → FastAPI → запись в БД (очередь) → отдельный процесс-контроллер
           → дочерний процесс на задачу → запись статуса в БД
```

Смысл: API отвечает мгновенно, выполнение durable (переживает рестарт API),
задачи выполняются параллельно (до 3 одновременно), статус отслеживается в БД.

**Первый пример задачи** — `fibonacci`: считает числа Фибоначчи с номерами
`left..right` и пишет их в Excel. Это заглушка, приближающая эксперимент к
реальному кейсу (сбор отчёта из данных), но не требующая внешних источников.

---

## 2. Технологический стек

- **Python ≥ 3.10** (используется синтаксис `int | None`, `dict[...]`).
- **FastAPI** + **uvicorn** — HTTP-слой.
- **SQLAlchemy 2.0 (async)** + **asyncpg** — БД (PostgreSQL).
- **Alembic** — миграции.
- **Polars** + xlsxwriter/openpyxl — построение и запись отчётов.
- **tsidpy** — генерация ID (TSID, time-sortable, 64-bit).
- **multiprocessing** (контекст `spawn`) — изоляция выполнения задач.

Управление зависимостями — **Poetry** (`pyproject.toml`, `poetry.lock`).

---

## 3. Структура проекта

```
main.py                          # FastAPI: lifespan (спавн контроллера) + HTTP-ручки
db.py                            # async engine + get_async_session()
models.py                        # Task, TaskQueue, TaskStatus (schema task_manager)
launcher.py                      # enqueue(name, params) -> task_id
tasks/
  __init__.py                    # импорт модулей задач (наполняет реестр)
  base.py                        # BaseTask (контракт задачи)
  registry.py                    # TASKS, register(), get_task()
  fibonacci.py                   # пример задачи
worker/
  __init__.py
  controller.py                  # controller_process_main + цикл оркестрации
  runner.py                      # run_task_process (target дочернего процесса)
alembic/versions/...             # миграции
reports/                         # сюда пишутся отчёты (task_{id}_{time()}.xlsx)
README.md                        # краткое описание архитектуры
SOLUTION.md                      # этот документ
```

---

## 4. Модель данных

Всё в схеме **`task_manager`** (задаётся в `MetaData(schema="task_manager")` в
`models.py`). Базовый класс `CommonModel` задаёт `id: BigInteger, primary_key,
autoincrement=False` — ID генерирует приложение (TSID), а не БД (см. §10.4).

### `task` — `Task`
| Поле | Тип | Смысл |
|---|---|---|
| `id` | BigInteger PK | TSID задачи |
| `name` | str | имя задачи в реестре (напр. `"fibonacci"`) |
| `params` | JSONB | параметры, переданные в задачу |

### `task_queue` — `TaskQueue`
| Поле | Тип | Смысл |
|---|---|---|
| `id` | BigInteger PK | TSID — **порядок постановки (FIFO)** |
| `task_id` | FK → task.id | какая задача поставлена |

Раньше был столбец `order` с `autoincrement=True`, но в PostgreSQL автоинкремент
на **не-PK** колонке не превращается в sequence и не заполняется. Убрали (см.
миграцию `8f3c1a7d92b4`); FIFO делаем по `task_queue.id` (TSID монотонен по
времени).

### `task_status` — `TaskStatus`
| Поле | Тип | Смысл |
|---|---|---|
| `id` | BigInteger PK | TSID |
| `task_id` | FK → task.id | какая задача |
| `status` | str | `pending` / `in_progress` / `success` / `failed` |
| `error` | str nullable | текст ошибки для `failed` |

**Одна строка на задачу**, `status` обновляется «на месте». Append-лог с
временем исполнения — намеренно отложен на будущее.

---

## 5. Поток выполнения (пошагово)

1. **HTTP**: клиент делает `POST /fibonacci` с телом `{left, right}`.
2. **Ручка** (`main.py`) вызывает `enqueue("fibonacci", {left, right})`.
3. **Launcher** (`launcher.py`):
   - `get_task(name)` — достаёт класс из реестра (отсекает неизвестные имена);
   - `validate_params(params)` — валидация/нормализация;
   - генерирует `task_id = TSID.create().number`;
   - в **одной сессии/транзакции** вставляет `Task` + `TaskQueue` + `TaskStatus(pending)`;
   - возвращает `task_id`.
4. **Ручка** отвечает `202` и `{"task_id": ...}`. Выполнения здесь нет.
5. **Контроллер** (`worker/controller.py`, отдельный процесс, запущен из
   `lifespan`) в цикле раз в секунду:
   - `reap_finished` — для завершённых дочерних процессов пишет `success`/`failed`;
   - `start_pending` — если есть свободные слоты (`MAX_CONCURRENT - len(running)`),
     берёт `pending`-задачи по `TaskQueue.id` (FIFO), помечает `in_progress`,
     спавнит дочерний процесс на каждую и **не ждёт** его;
6. **Дочерний процесс** (`worker/runner.run_task_process`) резолвит класс,
   инстанцирует `BaseTask(task_id, params)`, выполняет `asyncio.run(task.run())`.
   Успех → exit code 0, исключение → traceback в stderr и `sys.exit(1)`.
7. Контроллер на следующей итерации видит завершённый процесс и пишет финальный
   статус.

---

## 6. Жизненный цикл статуса

```
pending ──► in_progress ──► success
                       └──► failed (+ error)
```

| Статус | Кто ставит | Когда |
|---|---|---|
| `pending` | Launcher | при постановке в очередь |
| `in_progress` | Controller | перед спавном дочернего процесса |
| `success` | Controller | exit code == 0 |
| `failed` | Controller | exit code != 0 (в `error` пишется код) |

---

## 7. Компоненты: интерфейсы и детали

### 7.1. `tasks/base.py` — контракт задачи
```python
class BaseTask(ABC):
    def __init__(self, task_id: int, params: dict):
        self.task_id = task_id
        self.params = params

    @classmethod
    def validate_params(cls, raw: dict) -> dict:
        return raw                       # переопределяется при необходимости

    @abstractmethod
    async def run(self) -> None: ...
```
- `task_id` нужен задаче, например, для именования результата.
- `run()` **async** (см. §10.3).

### 7.2. `tasks/registry.py` — реестр
```python
TASKS: dict[str, type[BaseTask]] = {}

def register(name: str):        # декоратор на класс задачи
    ...
def get_task(name: str) -> type[BaseTask]:   # KeyError, если нет
    ...
```
Наполняется импортом модулей задач. `tasks/__init__.py` содержит
`from tasks import fibonacci  # noqa: F401`.

### 7.3. `launcher.py` — постановка в очередь
```python
async def enqueue(name: str, params: dict) -> int: ...
```
Работает в процессе FastAPI. Резолв класса, валидация, запись трёх строк в БД.
Возвращает `task_id`. HTTP-ручки должны быть тонкими: вызывают только это.

### 7.4. `worker/runner.py` — дочерний процесс задачи
```python
def run_task_process(task_id: int, name: str, params: dict) -> None: ...
```
Это **target** для `multiprocessing.Process`. Должна быть top-level функцией
(иначе spawn не сможет её запиклить). Внутри — `asyncio.run(task.run())`.

### 7.5. `worker/controller.py` — оркестратор
```python
POLL_INTERVAL = 1.0
MAX_CONCURRENT = 3

def controller_process_main() -> None:      # asyncio.run(controller_loop())
async def controller_loop() -> None:        # бесконечный цикл
async def recover_stuck_tasks() -> None:    # in_progress -> pending на старте
async def reap_finished(running) -> None:   # финализирует завершённые
async def finalize(task_id, exitcode):      # пишет success/failed+error
async def start_pending(ctx, running):      # добирает задачи по слотам
```
`running: dict[task_id, Process]` — какие задачи запущены этим контроллером.

### 7.6. `main.py` — FastAPI + lifespan
```python
@asynccontextmanager
async def lifespan(app):
    ctx = multiprocessing.get_context("spawn")
    controller = ctx.Process(target=controller_process_main)  # не daemon!
    controller.start()
    try:
        yield
    finally:
        controller.terminate(); controller.join()
```
Ручки: `GET /ping`, `POST /fibonacci` (202), `GET /tasks/{task_id}` (статус),
`GET /tasks?status=` (список, свежие первыми).

---

## 8. Конкурентность и оркестрация

Правило: **не более `MAX_CONCURRENT` (=3) задач одновременно**.

Алгоритм `controller_loop` на каждой итерации:
1. `reap_finished` — по каждому процессу: `is_alive()`; если завершился —
   `join()`, убрать из `running`, записать финальный статус.
2. `start_pending`:
   - `slots = MAX_CONCURRENT - len(running)`; если `<= 0` — выйти;
   - `SELECT` до `slots` задач, `pending`, `ORDER BY task_queue.id` (FIFO),
     `LIMIT slots`;
   - одним `UPDATE` пометить их `in_progress`;
   - заспавнить процесс на каждую, положить в `running`, **не дожидаясь**.
3. `sleep(POLL_INTERVAL)`.

Следствие: как задача завершается — её слот освобождается и на следующей
итерации доливается следующая из очереди.

`recover_stuck_tasks` при старте контроллера сбрасывает все `in_progress` в
`pending` — чтобы «зависшие» после падения задачи перезапустились.

---

## 9. Точка входа и запуск

- `python main.py` (или `uvicorn main:app`) — поднимает API и спавнит контроллера.
- БД должна быть применена: `alembic upgrade head`.
- URL БД задаётся в `db.py` (сейчас — **плейсхолдер**, см. §11.1) и в
  `alembic/env.py`.
- Ручная проверка: `POST /fibonacci {"left":1,"right":35}` → `task_id` →
  `GET /tasks/{task_id}` → наблюдать `pending → in_progress → success` →
  появится `reports/task_{id}_{time()}.xlsx`.

---

## 10. Ключевые решения и почему именно так

### 10.1. Задача — это код в реестре, а не запись в БД
В БД хранится только *постановка* (`name` + `params`). Сам код живёт в
`tasks/` и версионируется в git.
**Почему:** не нужно сериализовать/десериализовать код, безопаснее, эволюция
задач обычным деплоем. БД не превращается в хранилище логики.

### 10.2. Отдельный процесс-контроллер, спавнится из lifespan
FastAPI только принимает HTTP и пишет в БД. Оркестрацией занимается отдельный
процесс.
**Почему:** падение/нагрузка выполнения не роняет API; API отвечает мгновенно;
БД — durable-буфер. Запуск из `lifespan` удобен для эксперимента (не нужен
отдельный сервис/демон).

### 10.3. `run()` асинхронный
**Почему:** реальные задачи будут ходить в БД через asyncpg (получать данные
для отчётов). Async-контракт даёт им доступ к сессиям. Чистый CPU-код (polars)
внутри `async def` допустим: задача изолирована в своём процессе, блокировка
event loop никому не мешает.

### 10.4. TSID как первичный ключ (`autoincrement=False`)
**Почему:** ID генерируется приложением (не зависит от sequence БД),
time-sortable (`TSID.create().number` монотонно растёт), годится как FIFO-порядок
в очереди. Убирает round-trip к БД за `nextval`.

### 10.5. Каждая задача — отдельный процесс (`spawn`)
**Почему:**
- изоляция: падение одной задачи не роняет контроллер (exit code — сигнал);
- `spawn` (а не `fork`) — чтобы дочерний процесс **не наследовал** event loop
  FastAPI и открытые asyncpg-соединения (иначе классические проблемы с
  сокетами/лупами);
- контроллер запускает процесс и **не блокируется** — так достижима
  параллельность.
Контроллер обязан быть **не daemon** — демонические процессы не могут иметь
детей (`multiprocessing` это запрещает).

### 10.6. Статусы пишет контроллер, а не задача
**Почему:** единая точка правды о жизненном цикле; задача не должна знать про
очередь и другие задачи. Дочерний процесс занят только логикой и возвращает
успех/провал через exit code.

### 10.7. Конкурентность через `len(running)` в памяти
**Почему:** просто и достаточно для эксперимента. При рестарте счётчик
восстанавливается через `recover_stuck_tasks`. Полноценный брокер/локи в БД —
избыточны на этом этапе.

### 10.8. Одна строка статуса на задачу
**Почему:** простые запросы «есть ли running», минимум кода. Полноценный
append-лог (с `started_at`/`finished_at`/результатом) запланирован на будущее,
сейчас сознательно не делаем.

### 10.9. Ручка на каждую задачу + общие `/tasks`
**Почему:** в реальном кейсе на каждый отчёт будет своя ручка (`/sales_report`,
`/users_export`, ...), а `/tasks` и `/tasks/{id}` — общий мониторинг. `POST`
возвращает `202 Accepted` + `task_id` (выполнение фоновое).

---

## 11. Ограничения, известные каверзы

1. **URL БД — плейсхолдер.** В `db.py` и `alembic/env.py` стоит заглушка
   `postgresql+asyncpg://user:password@/database?...`. Для реального запуска
   подставь рабочий DSN (лучше — вынести в ENV).
2. **`uvicorn --reload` может поднять несколько контроллеров** (несколько
   экземпляров приложения). На рабочую БД должен быть **один** контроллер.
3. **Зависание между commit и spawn.** Если контроллер упадёт сразу после
   пометки `in_progress` и до спавна, задача останется `in_progress` до
   перезапуска контроллера (тогда `recover_stuck_tasks` вернёт её в `pending`).
4. **Нет ретраев, таймаутов, отмены.** Проваленная задача не повторяется.
5. **Путь `reports/` относительный** — считается от CWD процесса контроллера.
6. **Excel и большие числа.** Excel-число держит ~15 значащих цифр, поэтому
   в `fibonacci` значения > `Int64` пишутся **строкой** (`pl.Utf8`) — иначе
   теряется точность (см. `tasks/fibonacci.py`).

---

## 12. Конвенции кода

- Стиль проекта — минимализм: **не добавлять комментарии без необходимости**.
- Схема БД — всегда `task_manager`.
- ID новых строк — `TSID.create().number`.
- Сессии — только через `db.get_async_session()` (async context manager,
  откатывает при исключении).
- Циклы/side-effect «на месте» — через ORM `update(...).execution_options(
  synchronize_session=False)`.
- Новые задачи регистрируются декоратором `@register("<name>")`; имя задачи
  совпадает с именем ручки и с `name` в `enqueue(...)`.

---

## 13. Чек-лист: как добавить НОВУЮ задачу (главное)

Допустим, новая задача называется `sales_report` и получает `date_from`,
`date_to`.

1. **Создать класс задачи** `tasks/sales_report.py`:
   ```python
   from tasks.base import BaseTask
   from tasks.registry import register

   @register("sales_report")
   class SalesReportTask(BaseTask):
       @classmethod
       def validate_params(cls, raw: dict) -> dict:
           date_from = str(raw["date_from"])
           date_to = str(raw["date_to"])
           # ... проверки, брось ValueError при неверных данных
           return {"date_from": date_from, "date_to": date_to}

       async def run(self) -> None:
           date_from = self.params["date_from"]
           date_to = self.params["date_to"]
           # ... тут логика. Если нужна БД:
           #     from db import get_async_session
           #     async with get_async_session() as session: ...
           # результат — файл в reports/, имя: task_{self.task_id}_{int(time())}.xlsx
   ```
2. **Зарегистрировать модуль**: в `tasks/__init__.py` добавить
   `from tasks import sales_report  # noqa: F401` (иначе реестр пуст для неё).
3. **Добавить HTTP-ручку** в `main.py`:
   - pydantic-модель запроса;
   - `POST /sales_report` со статусом 202, который вызывает
     `await enqueue("sales_report", {...})` и возвращает `{"task_id": ...}`;
   - ошибки валидации (`ValueError`) → `HTTPException(422, ...)`.
4. **Ничего больше не трогать**: очередь, статусы, контроллер, конкурентность и
   ручки `/tasks*` уже общие и работают для любой задачи автоматически.
5. **Проверить**: поставить задачу, посмотреть `GET /tasks/{id}`, дождаться
   `success`, проверить, что отчёт появился в `reports/`. Проверить и
   ошибочный сценарий (`failed` + `error`).

### Если задача читает/пишет БД
- Внутри `async def run()` используй `async with get_async_session() as session`.
  Сессия создаётся в **дочернем процессе** — это нормально, процесс свой.
- Не тащи объекты ORM из контроллера в задачу: в задачу передаются только
  `task_id` (int) и `params` (dict из `Task.params`, JSONB).

### Если задача долгая (для ручной отладки статусов)
- В пример `fibonacci` добавлены `SLEEP_EVERY`/`SLEEP_SECONDS` (сон 1с каждые
  10 чисел). Можешь варьировать, чтобы успеть посмотреть статусы.

---

## 14. Что можно улучшить дальше (не сейчас)

- Вынести URL БД/`MAX_CONCURRENT`/`POLL_INTERVAL` в конфиг/ENV.
- Append-лог статусов с `started_at`/`finished_at`/результатом.
- Ретраи, таймауты, отмена, приоритеты.
- `LISTEN/NOTIFY` PostgreSQL вместо поллинга.
- Отдельный демон-воркер вместо `lifespan`-спавна (когда сервис вырастет).
- Тесты: юниты на `validate_params`, интеграционный прогон launcher→controller
  на временной БД.

---

## 15. Быстрая карта кода

| Хочу... | Смотреть |
|---|---|
| Понять контракт задачи | `tasks/base.py` |
| Добавить задачу | `tasks/*.py` + `tasks/__init__.py` + `main.py` |
| Понять постановку в очередь | `launcher.py` |
| Понять оркестрацию/уровень параллелизма | `worker/controller.py` |
| Понять выполнение в процессе | `worker/runner.py` |
| Понять схему БД | `models.py` + `alembic/versions/` |
| Понять запуск/ручки | `main.py` |
| Понять подключение к БД | `db.py`, `alembic/env.py` |
