import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base


class FullScan(Base):
  __tablename__ = "full_scans"

  id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
  user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
  domain = Column(String(255), index=True, nullable=False)
  full_report_json = Column(JSONB, nullable=False)
  created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
