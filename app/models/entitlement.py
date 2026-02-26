import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class Entitlement(Base):
  """One row per user: credits and subscription state."""
  __tablename__ = "entitlements"

  id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
  user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
  credits_remaining = Column(Integer, nullable=False, default=0)
  monthly_quota = Column(Integer, nullable=True)  # for subscriptions
  subscription_active = Column(Boolean, nullable=False, default=False)
  subscription_renewal_date = Column(DateTime(timezone=True), nullable=True)
  stripe_customer_id = Column(String(255), nullable=True)
  created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
  updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

  user = relationship("User", backref="entitlement")
