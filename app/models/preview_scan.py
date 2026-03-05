import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base


class PreviewScan(Base):
  __tablename__ = "preview_scans"

  id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
  domain = Column(String(255), unique=True, index=True, nullable=False)
  preview_json = Column(JSONB, nullable=False)
  created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
  expires_at = Column(DateTime(timezone=True), nullable=False)
