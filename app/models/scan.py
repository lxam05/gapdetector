import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base


class Scan(Base):
  __tablename__ = "scans"

  id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
  company_name = Column(String(255), index=True, nullable=False)
  summary = Column(Text, nullable=False)
  biggest_weakness = Column(Text, nullable=False)
  key_complaints = Column(JSONB, nullable=False)
  missed_opportunities = Column(JSONB, nullable=False)
  premium_analysis = Column(Text, nullable=False)
  full_analysis = Column(JSONB, nullable=True)  # full app-style dashboard payload when real analyzer ran
  is_unlocked = Column(Boolean, nullable=False, default=False)
  created_at = Column(
    DateTime(timezone=True),
    default=datetime.utcnow,
    nullable=False,
  )
  user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
  credits_used = Column(Boolean, nullable=False, default=False)

