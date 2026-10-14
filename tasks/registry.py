from tasks.base import BaseTask

TASKS: dict[str, type[BaseTask]] = {}


def register(name: str):
    def wrap(cls: type[BaseTask]) -> type[BaseTask]:
        TASKS[name] = cls
        return cls
    return wrap


def get_task(name: str) -> type[BaseTask]:
    return TASKS[name]
