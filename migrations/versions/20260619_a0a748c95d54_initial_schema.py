"""initial schema

Revision ID: a0a748c95d54
Revises: 
Create Date: 2026-06-19 01:39:05.465752
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a0a748c95d54'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('repo',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('git_url', sa.String(), nullable=False),
    sa.Column('default_base', sa.String(), nullable=False),
    sa.Column('env_file', sa.String(), nullable=True),
    sa.Column('creds_volume', sa.String(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('repo_id', sa.String(), nullable=False),
    sa.Column('workflow', sa.String(), nullable=False),
    sa.Column('state', sa.String(), nullable=False),
    sa.Column('turn', sa.String(), nullable=False),
    sa.Column('blocked', sa.Boolean(), nullable=False),
    sa.Column('slug', sa.String(), nullable=True),
    sa.Column('branch', sa.String(), nullable=True),
    sa.Column('clone', sa.String(), nullable=True),
    sa.Column('claimed_by', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['repo_id'], ['repo.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('history',
    sa.Column('task_id', sa.String(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('at', sa.String(), nullable=False),
    sa.Column('from_state', sa.String(), nullable=True),
    sa.Column('to_state', sa.String(), nullable=False),
    sa.Column('trigger', sa.String(), nullable=True),
    sa.Column('note', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['task.id'], ),
    sa.PrimaryKeyConstraint('task_id', 'seq')
    )
    op.create_table('responsibility',
    sa.Column('task_id', sa.String(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('idx', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('comment', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['task_id', 'seq'], ['history.task_id', 'history.seq'], ),
    sa.PrimaryKeyConstraint('task_id', 'seq', 'idx')
    )


def downgrade() -> None:
    op.drop_table('responsibility')
    op.drop_table('history')
    op.drop_table('task')
    op.drop_table('repo')
