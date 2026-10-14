from tsidpy import TSID

from db import get_async_session
from models import Task, TaskQueue, TaskStatus
from tasks.registry import get_task


async def enqueue(name: str, params: dict) -> int:
    task_cls = get_task(name)
    validated = task_cls.validate_params(params)

    task_id = TSID.create().number

    async with get_async_session() as session:
        session.add(Task(id=task_id, name=name, params=validated))
        session.add(TaskQueue(id=TSID.create().number, task_id=task_id))
        session.add(TaskStatus(id=TSID.create().number, task_id=task_id, status="pending"))
        await session.commit()

    return task_id
