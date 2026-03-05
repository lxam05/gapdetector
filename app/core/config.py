from functools import lru_cache
from pydantic import AliasChoices, AnyUrl, EmailStr, Field
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

  # Existing analyzer integration (optional, but present in env)
  SERPER_API_KEY: str | None = None
  ANTHROPIC_API_KEY: str | None = None
  ANTHROPIC_MODEL: str | None = None
  OPENAI_API_KEY: str | None = None
  OPENAI_PREVIEW_MODEL: str = "gpt-4o-mini"

  # Preview protection
  TURNSTILE_SECRET_KEY: str | None = None
  TURNSTILE_SITE_KEY: str | None = None
  REDIS_URL: str | None = None

  # Cost and throughput guardrails
  PREVIEW_IP_PER_DAY_LIMIT: int = 2
  PREVIEW_IP_PER_5M_LIMIT: int = 1
  PREVIEW_DOMAIN_REPEAT_BLOCK_SECONDS: int = 600
  PREVIEW_GLOBAL_DAILY_LIMIT: int = 20
  FULL_GLOBAL_DAILY_LIMIT: int = 10
  MAX_MONTHLY_API_CALLS: int = 3000

  # MailerSend (contact form)
  MAILERSEND_API_KEY: str | None = None
  MAILERSEND_FROM_EMAIL: str | None = None
  MAILERSEND_TO_EMAIL: str | None = None

  # Stripe paywall: dynamic Checkout Session (success_url includes scan_id)
  STRIPE_SECRET_KEY: str | None = None
  STRIPE_WEBHOOK_SECRET: str | None = None  # webhook signing secret (whsec_...)
  STRIPE_PRICE_ID: str | None = None  # legacy single-report price
  PRICE_SINGLE_REPORT: str | None = None   # €4.99 one-time 1 credit
  PRICE_BUNDLE_5: str | None = Field(default=None, validation_alias=AliasChoices("PRICE_BUNDLE_5", "STRIPE_BUNDLE_5"))
  PRICE_SUB_MONTHLY: str | None = Field(default=None, validation_alias=AliasChoices("PRICE_SUB_MONTHLY", "STRIPE_SUB_MONTHLY"))
  STRIPE_PAYMENT_LINK_URL: str = "https://buy.stripe.com/test_5kQ6oH9vd7a42ai7bA5c400"
  SUCCESS_URL_BASE: str = "http://localhost:8000"

  class Config:
    env_file = ".env"
    case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
  return Settings()


settings = get_settings()

