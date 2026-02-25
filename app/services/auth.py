from datetime import timedelta
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
  create_access_token,
  create_email_token,
  create_refresh_token,
  get_token_subject,
  hash_password,
  verify_password,
)
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.email import send_email


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
  stmt = select(User).where(User.email == email.lower())
  result = await db.execute(stmt)
  return result.scalar_one_or_none()


async def register_user(db: AsyncSession, payload: RegisterRequest) -> User:
  email = payload.email.lower()
  existing = await get_user_by_email(db, email)
  if existing:
    # Do not leak enumeration details; generic error.
    raise HTTPException(
      status_code=status.HTTP_400_BAD_REQUEST,
      detail="Unable to register with that email.",
    )

  user = User(
    email=email,
    hashed_password=hash_password(payload.password),
    is_active=True,
    is_verified=False,
  )
  db.add(user)
  await db.commit()
  await db.refresh(user)

  # Send verification email (no tokens issued yet)
  token = create_email_token(str(user.id), minutes=60 * 24, scope="verify")
  verify_link = f"{settings.GOOGLE_REDIRECT_URI.rsplit('/auth', 1)[0]}/auth/verify-email?token={token}"
  send_email(
    to=user.email,
    subject="Verify your GapDetector account",
    html_body=f"<p>Click to verify your email:</p><p><a href='{verify_link}'>Verify email</a></p>",
  )

  return user


async def verify_email_token(db: AsyncSession, token: str) -> None:
  try:
    user_id = get_token_subject(token, expected_scope="verify")
  except ValueError:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token.")

  user = await db.get(User, user_id)
  if not user:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token.")

  if not user.is_verified:
    user.is_verified = True
    await db.commit()


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User:
  user = await get_user_by_email(db, email.lower())
  if not user or not user.hashed_password:
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Incorrect email or password.",
    )
  if not verify_password(password, user.hashed_password):
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Incorrect email or password.",
    )
  if not user.is_verified:
    raise HTTPException(
      status_code=status.HTTP_403_FORBIDDEN,
      detail="Email is not verified.",
    )
  return user


async def login_user(db: AsyncSession, payload: LoginRequest) -> dict:
  user = await authenticate_user(db, payload.email, payload.password)
  access = create_access_token(str(user.id))
  refresh = create_refresh_token(str(user.id))
  return {
    "access_token": access,
    "refresh_token": refresh,
    "token_type": "bearer",
  }


def refresh_tokens(refresh_token: str) -> dict:
  try:
    user_id = get_token_subject(refresh_token, expected_scope="refresh")
  except ValueError:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.")

  access = create_access_token(user_id)
  new_refresh = create_refresh_token(user_id)
  return {
    "access_token": access,
    "refresh_token": new_refresh,
    "token_type": "bearer",
  }


async def request_password_reset(db: AsyncSession, email: str) -> None:
  user = await get_user_by_email(db, email.lower())
  if not user:
    # Avoid user enumeration
    return
  token = create_email_token(str(user.id), minutes=60, scope="reset")
  reset_link = f"{settings.GOOGLE_REDIRECT_URI.rsplit('/auth', 1)[0]}/auth/reset-password?token={token}"
  send_email(
    to=user.email,
    subject="Reset your GapDetector password",
    html_body=f"<p>Click to reset your password:</p><p><a href='{reset_link}'>Reset password</a></p>",
  )


async def reset_password(db: AsyncSession, token: str, new_password: str) -> None:
  try:
    user_id = get_token_subject(token, expected_scope="reset")
  except ValueError:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token.")

  user = await db.get(User, user_id)
  if not user:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token.")

  user.hashed_password = hash_password(new_password)
  await db.commit()

