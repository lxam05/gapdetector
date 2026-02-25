from datetime import datetime

from pydantic import BaseModel, EmailStr


class UserBase(BaseModel):
  email: EmailStr


class UserCreate(UserBase):
  password: str


class UserOut(UserBase):
  id: str
  is_active: bool
  is_verified: bool
  created_at: datetime

  class Config:
    orm_mode = True

