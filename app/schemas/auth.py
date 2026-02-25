from pydantic import BaseModel, EmailStr


class RegisterRequest(BaseModel):
  email: EmailStr
  password: str


class LoginRequest(BaseModel):
  email: EmailStr
  password: str


class TokenPair(BaseModel):
  access_token: str
  refresh_token: str
  token_type: str = "bearer"


class RefreshRequest(BaseModel):
  refresh_token: str


class ForgotPasswordRequest(BaseModel):
  email: EmailStr


class ResetPasswordRequest(BaseModel):
  token: str
  new_password: str

