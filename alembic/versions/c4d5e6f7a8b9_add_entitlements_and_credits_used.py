"""add entitlements and scan credits_used

Revision ID: c4d5e6f7a8b9
Revises: b3c1e1f2c9ab
Create Date: 2026-02-25 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c1e1f2c9ab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  op.create_table(
    "entitlements",
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
    sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("credits_remaining", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("monthly_quota", sa.Integer(), nullable=True),
    sa.Column("subscription_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    sa.Column("subscription_renewal_date", sa.DateTime(timezone=True), nullable=True),
    sa.Column("stripe_customer_id", sa.String(length=255), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
  )
  op.create_index("ix_entitlements_user_id", "entitlements", ["user_id"], unique=True)

  op.add_column("scans", sa.Column("credits_used", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
  # Use IF EXISTS so downgrade succeeds even if upgrade never ran (e.g. DB was stamped but migration failed)
  conn = op.get_bind()
  conn.execute(sa.text("ALTER TABLE scans DROP COLUMN IF EXISTS credits_used"))
  conn.execute(sa.text("DROP INDEX IF EXISTS ix_entitlements_user_id"))
  conn.execute(sa.text("DROP TABLE IF EXISTS entitlements"))
