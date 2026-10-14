from pathlib import Path
from time import sleep, time

import polars as pl

from tasks.base import BaseTask
from tasks.registry import register

REPORTS_DIR = Path("reports")

MAX_INT64 = 2 ** 63 - 1

SLEEP_EVERY = 10
SLEEP_SECONDS = 1.0


@register("fibonacci")
class FibonacciTask(BaseTask):

    @classmethod
    def validate_params(cls, raw: dict) -> dict:
        left = int(raw["left"])
        right = int(raw["right"])
        if left < 1:
            raise ValueError("left must be >= 1")
        if right < left:
            raise ValueError("right must be >= left")
        return {"left": left, "right": right}

    async def run(self) -> None:
        left = self.params["left"]
        right = self.params["right"]

        a, b = 0, 1
        sequence = []
        for i in range(1, right + 1):
            a, b = b, a + b
            sequence.append(a)
            if i % SLEEP_EVERY == 0:
                sleep(SLEEP_SECONDS)

        fibs = sequence[left - 1:right]

        if fibs and max(fibs) > MAX_INT64:
            fib_col = pl.Series("fib", [str(f) for f in fibs], dtype=pl.Utf8)
        else:
            fib_col = pl.Series("fib", fibs, dtype=pl.Int64)

        REPORTS_DIR.mkdir(exist_ok=True)
        output = REPORTS_DIR / f"task_{self.task_id}_{int(time())}.xlsx"

        df = pl.DataFrame({
            "n": pl.Series(range(left, right + 1), dtype=pl.Int64),
            "fib": fib_col,
        })
        df.write_excel(output)
