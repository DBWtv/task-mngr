import asyncio
import sys
import traceback

from tasks.registry import get_task


def run_task_process(task_id: int, name: str, params: dict) -> None:
    task_cls = get_task(name)
    task = task_cls(task_id, params)
    try:
        asyncio.run(task.run())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
