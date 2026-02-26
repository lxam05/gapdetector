from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr


class UserBase(BaseModel):
  email: EmailStr


class UserCreate(UserBase):
  password: str


class UserOut(UserBase):
  id: UUID
  is_active: bool
  is_verified: bool
  created_at: datetime

  class Config:
    from_attributes = True

