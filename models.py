from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)
from sqlalchemy import BigInteger, String, ForeignKey, MetaData
from sqlalchemy.dialects.postgresql import JSONB

class CommonModel(DeclarativeBase):
    
    __abstract__ = True
    
    metadata = MetaData(schema="task_manager")
    
    id: Mapped[int] = mapped_column(
        BigInteger(),
        primary_key=True,
        autoincrement=False,
    )


class Task(CommonModel):
    
    __tablename__ = "task"
    
    name: Mapped[str] = mapped_column(String(), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB(), nullable=False)


class TaskQueue(CommonModel):
    
    __tablename__ = "task_queue"
    
    task_id: Mapped[int] = mapped_column(
        ForeignKey("task.id")
    )
    order: Mapped[int] = mapped_column(autoincrement=True)


class TaskStatus(CommonModel):
    
    __tablename__ = "task_status"
    
    task_id: Mapped[int] = mapped_column(
        ForeignKey("task.id")
    )
    status: Mapped[str] = mapped_column(String(), nullable=False)
