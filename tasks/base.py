from abc import ABC, abstractmethod


class BaseTask(ABC):

    def __init__(self, task_id: int, params: dict):
        self.task_id = task_id
        self.params = params

    @classmethod
    def validate_params(cls, raw: dict) -> dict:
        return raw

    @abstractmethod
    async def run(self) -> None:
        ...
