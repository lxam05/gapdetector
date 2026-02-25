import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class User(Base):
  __tablename__ = "users"

  id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
  email = Column(String(255), unique=True, index=True, nullable=False)
  hashed_password = Column(String(255), nullable=True)  # nullable for OAuth-only accounts
  is_active = Column(Boolean, nullable=False, default=True)
  is_verified = Column(Boolean, nullable=False, default=False)

  google_id = Column(String(255), unique=True, nullable=True)

  stripe_customer_id = Column(String(255), nullable=True)
  stripe_subscription_id = Column(String(255), nullable=True)

  created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
  updated_at = Column(
    DateTime(timezone=True),
    default=datetime.utcnow,
    onupdate=datetime.utcnow,
    nullable=False,
  )

