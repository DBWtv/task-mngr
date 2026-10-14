import multiprocessing
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from db import get_async_session
from launcher import enqueue
from models import Task, TaskStatus
from worker.controller import controller_process_main


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = multiprocessing.get_context("spawn")
    controller = ctx.Process(target=controller_process_main)
    controller.start()
    try:
        yield
    finally:
        controller.terminate()
        controller.join()


app = FastAPI(lifespan=lifespan)


class FibonacciRequest(BaseModel):
    left: int
    right: int


@app.get("/ping")
async def ping():
    return {"status": "ok"}


@app.post("/fibonacci", status_code=202)
async def create_fibonacci(payload: FibonacciRequest):
    try:
        task_id = await enqueue(
            "fibonacci",
            {"left": payload.left, "right": payload.right},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"task_id": task_id}


@app.get("/tasks/{task_id}")
async def get_task(task_id: int):
    async with get_async_session() as session:
        row = (
            await session.execute(
                select(Task, TaskStatus)
                .join(TaskStatus, TaskStatus.task_id == Task.id)
                .where(Task.id == task_id)
            )
        ).first()

    if row is None:
        raise HTTPException(status_code=404, detail="task not found")

    task, status = row
    return {
        "task_id": task.id,
        "name": task.name,
        "params": task.params,
        "status": status.status,
        "error": status.error,
    }


@app.get("/tasks")
async def list_tasks(status: str | None = None):
    query = (
        select(Task, TaskStatus)
        .join(TaskStatus, TaskStatus.task_id == Task.id)
        .order_by(Task.id.desc())
    )
    if status is not None:
        query = query.where(TaskStatus.status == status)

    async with get_async_session() as session:
        rows = (await session.execute(query)).all()

    return {
        "tasks": [
            {
                "task_id": task.id,
                "name": task.name,
                "status": status_row.status,
                "error": status_row.error,
            }
            for task, status_row in rows
        ]
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8081,
        reload=True,
    )
