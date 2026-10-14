"""add error to task_status, drop order from task_queue

Revision ID: 8f3c1a7d92b4
Revises: 372160f33e69
Create Date: 2026-10-14 11:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f3c1a7d92b4'
down_revision: Union[str, Sequence[str], None] = '372160f33e69'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'task_status',
        sa.Column('error', sa.String(), nullable=True),
        schema='task_manager',
    )
    op.drop_column('task_queue', 'order', schema='task_manager')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'task_queue',
        sa.Column('order', sa.Integer(), autoincrement=True, nullable=False),
        schema='task_manager',
    )
    op.drop_column('task_status', 'error', schema='task_manager')
