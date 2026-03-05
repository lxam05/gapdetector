"""add preview/full scans and API usage logs

Revision ID: e7f8a9b0c1d2
Revises: c4d5e6f7a8b9
Create Date: 2026-02-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  op.create_table(
    "preview_scans",
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
    sa.Column("domain", sa.String(length=255), nullable=False),
    sa.Column("preview_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
  )
  op.create_index("ix_preview_scans_domain", "preview_scans", ["domain"], unique=True)

  op.create_table(
    "full_scans",
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
    sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("domain", sa.String(length=255), nullable=False),
    sa.Column("full_report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
  )
  op.create_index("ix_full_scans_user_id", "full_scans", ["user_id"], unique=False)
  op.create_index("ix_full_scans_domain", "full_scans", ["domain"], unique=False)

  op.create_table(
    "api_usage_logs",
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
    sa.Column("scan_kind", sa.String(length=16), nullable=False),
    sa.Column("scan_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("domain", sa.String(length=255), nullable=False),
    sa.Column("provider", sa.String(length=64), nullable=False),
    sa.Column("operation", sa.String(length=128), nullable=False),
    sa.Column("input_tokens", sa.Integer(), nullable=True),
    sa.Column("output_tokens", sa.Integer(), nullable=True),
    sa.Column("cost_usd", sa.Float(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
  )
  op.create_index("ix_api_usage_logs_domain", "api_usage_logs", ["domain"], unique=False)


def downgrade() -> None:
  op.drop_index("ix_api_usage_logs_domain", table_name="api_usage_logs")
  op.drop_table("api_usage_logs")

  op.drop_index("ix_full_scans_domain", table_name="full_scans")
  op.drop_index("ix_full_scans_user_id", table_name="full_scans")
  op.drop_table("full_scans")

  op.drop_index("ix_preview_scans_domain", table_name="preview_scans")
  op.drop_table("preview_scans")
