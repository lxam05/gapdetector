import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class ApiUsageLog(Base):
  __tablename__ = "api_usage_logs"

  id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
  scan_kind = Column(String(16), nullable=False)  # preview | full
  scan_id = Column(UUID(as_uuid=True), nullable=True)
  domain = Column(String(255), nullable=False, index=True)
  provider = Column(String(64), nullable=False)
  operation = Column(String(128), nullable=False)
  input_tokens = Column(Integer, nullable=True)
  output_tokens = Column(Integer, nullable=True)
  cost_usd = Column(Float, nullable=True)
  created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
