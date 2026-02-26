"""add scans table

Revision ID: b3c1e1f2c9ab
Revises: 2ee914c361b7
Create Date: 2026-02-25 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b3c1e1f2c9ab"
down_revision: Union[str, None] = "2ee914c361b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  op.create_table(
    "scans",
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
    sa.Column("company_name", sa.String(length=255), nullable=False, index=True),
    sa.Column("summary", sa.Text(), nullable=False),
    sa.Column("biggest_weakness", sa.Text(), nullable=False),
    sa.Column("key_complaints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("missed_opportunities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("premium_analysis", sa.Text(), nullable=False),
    sa.Column("is_unlocked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
  )
  op.create_index("ix_scans_company_name", "scans", ["company_name"])


def downgrade() -> None:
  op.drop_index("ix_scans_company_name", table_name="scans")
  op.drop_table("scans")

