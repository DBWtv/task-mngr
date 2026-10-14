import asyncio
import multiprocessing

from sqlalchemy import select, update

from db import get_async_session
from models import Task, TaskQueue, TaskStatus
from worker.runner import run_task_process

POLL_INTERVAL = 1.0
MAX_CONCURRENT = 3


def controller_process_main() -> None:
    asyncio.run(controller_loop())


async def controller_loop() -> None:
    await recover_stuck_tasks()

    ctx = multiprocessing.get_context("spawn")
    running: dict[int, multiprocessing.Process] = {}

    while True:
        try:
            await reap_finished(running)
            await start_pending(ctx, running)
        except Exception:
            import traceback
            traceback.print_exc()
        await asyncio.sleep(POLL_INTERVAL)


async def recover_stuck_tasks() -> None:
    async with get_async_session() as session:
        await session.execute(
            update(TaskStatus)
            .where(TaskStatus.status == "in_progress")
            .values(status="pending")
            .execution_options(synchronize_session=False)
        )
        await session.commit()


async def reap_finished(running: dict[int, multiprocessing.Process]) -> None:
    for task_id, proc in list(running.items()):
        if proc.is_alive():
            continue
        proc.join()
        exitcode = proc.exitcode
        del running[task_id]
        await finalize(task_id, exitcode)
        print(f"task {task_id} -> {'success' if exitcode == 0 else f'failed ({exitcode})'}")


async def finalize(task_id: int, exitcode: int | None) -> None:
    async with get_async_session() as session:
        status = await session.scalar(
            select(TaskStatus).where(TaskStatus.task_id == task_id)
        )
        if exitcode == 0:
            status.status = "success"
            status.error = None
        else:
            status.status = "failed"
            status.error = f"process exited with code {exitcode}"
        await session.commit()


async def start_pending(
    ctx: multiprocessing.context.BaseContext,
    running: dict[int, multiprocessing.Process],
) -> None:
    slots = MAX_CONCURRENT - len(running)
    if slots <= 0:
        return

    async with get_async_session() as session:
        tasks = (
            await session.execute(
                select(Task)
                .join(TaskQueue, TaskQueue.task_id == Task.id)
                .join(TaskStatus, TaskStatus.task_id == Task.id)
                .where(TaskStatus.status == "pending")
                .order_by(TaskQueue.id)
                .limit(slots)
            )
        ).scalars().all()

        if not tasks:
            return

        await session.execute(
            update(TaskStatus)
            .where(TaskStatus.task_id.in_([t.id for t in tasks]))
            .values(status="in_progress")
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    for task in tasks:
        proc = ctx.Process(
            target=run_task_process,
            args=(task.id, task.name, task.params),
        )
        proc.start()
        running[task.id] = proc
        print(f"task {task.id} -> in_progress ({len(running)}/{MAX_CONCURRENT})")
