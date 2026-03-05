import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class UserScan(Base):
  __tablename__ = "user_scans"

  id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
  user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
  preview_id = Column(UUID(as_uuid=True), ForeignKey("preview_scans.id", ondelete="CASCADE"), nullable=False, index=True)
  created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

  __table_args__ = (
    UniqueConstraint("user_id", "preview_id", name="uq_user_scans_user_preview"),
  )

