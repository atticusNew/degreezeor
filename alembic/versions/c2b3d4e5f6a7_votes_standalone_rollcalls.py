"""votes: support standalone roll calls (nullable action_id + metadata columns)

Revision ID: c2b3d4e5f6a7
Revises: b1a2c3d4e5f6
Create Date: 2026-06-08

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2b3d4e5f6a7"
down_revision: str | Sequence[str] | None = "b1a2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("votes") as batch:
        batch.alter_column("action_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("congress", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("roll_call", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("bill_number", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("category", sa.String(length=40), nullable=True))
    op.create_index("ix_votes_chamber_congress_roll", "votes",
                    ["chamber", "congress", "roll_call"])
    # Per-official voting record reads vote_positions by official_id (millions of rows).
    op.create_index("ix_vote_positions_official_id", "vote_positions", ["official_id"])
    op.create_index("ix_vote_positions_vote_id", "vote_positions", ["vote_id"])


def downgrade() -> None:
    op.drop_index("ix_vote_positions_vote_id", table_name="vote_positions")
    op.drop_index("ix_vote_positions_official_id", table_name="vote_positions")
    op.drop_index("ix_votes_chamber_congress_roll", table_name="votes")
    with op.batch_alter_table("votes") as batch:
        batch.drop_column("category")
        batch.drop_column("bill_number")
        batch.drop_column("roll_call")
        batch.drop_column("congress")
        batch.alter_column("action_id", existing_type=sa.Integer(), nullable=False)
