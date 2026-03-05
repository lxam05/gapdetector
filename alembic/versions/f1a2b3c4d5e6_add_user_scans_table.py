"""add user_scans table to link previews to users

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-03-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  op.create_table(
    "user_scans",
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
    sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("preview_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("preview_scans.id", ondelete="CASCADE"), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
  )
  op.create_index("ix_user_scans_user_id", "user_scans", ["user_id"], unique=False)
  op.create_index("ix_user_scans_preview_id", "user_scans", ["preview_id"], unique=False)
  op.create_unique_constraint("uq_user_scans_user_preview", "user_scans", ["user_id", "preview_id"])


def downgrade() -> None:
  op.drop_constraint("uq_user_scans_user_preview", "user_scans", type_="unique")
  op.drop_index("ix_user_scans_preview_id", table_name="user_scans")
  op.drop_index("ix_user_scans_user_id", table_name="user_scans")
  op.drop_table("user_scans")

