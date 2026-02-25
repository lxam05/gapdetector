from functools import lru_cache
from pydantic import AnyUrl, EmailStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
  """Application settings loaded from environment variables.

  Works both locally (via .env) and on Railway (env injected).
  """

  # Core
  DATABASE_URL: AnyUrl

  # JWT
  JWT_SECRET_KEY: str
  JWT_ALGORITHM: str = "HS256"
  ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
  REFRESH_TOKEN_EXPIRE_DAYS: int = 14

  # Google OAuth
  GOOGLE_CLIENT_ID: str
  GOOGLE_CLIENT_SECRET: str
  GOOGLE_REDIRECT_URI: str

  # Email
  EMAIL_FROM: EmailStr
  SMTP_HOST: str = ""
  SMTP_PORT: int = 587
  SMTP_USERNAME: str = ""
  SMTP_PASSWORD: str = ""
  SMTP_TLS: bool = True

  # CORS
  CORS_ORIGINS: str = "*"  # comma-separated list; tune in production

  class Config:
    env_file = ".env"
    case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
  return Settings()


settings = get_settings()

