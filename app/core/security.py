from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
  return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
  return pwd_context.verify(plain_password, hashed_password)


def _create_token(
  subject: Union[str, int],
  expires_delta: timedelta,
  scope: Optional[str] = None,
) -> str:
  now = datetime.now(timezone.utc)
  to_encode = {
    "sub": str(subject),
    "iat": int(now.timestamp()),
    "exp": int((now + expires_delta).timestamp()),
  }
  if scope:
    to_encode["scope"] = scope
  return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: Union[str, int]) -> str:
  return _create_token(
    user_id,
    timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    scope="access",
  )


def create_refresh_token(user_id: Union[str, int]) -> str:
  return _create_token(
    user_id,
    timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    scope="refresh",
  )


def create_email_token(user_id: Union[str, int], minutes: int, scope: str) -> str:
  return _create_token(user_id, timedelta(minutes=minutes), scope=scope)


def decode_token(token: str) -> dict:
  return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


def get_token_subject(token: str, expected_scope: Optional[str] = None) -> str:
  try:
    payload = decode_token(token)
  except JWTError:
    raise ValueError("Invalid token")
  sub = payload.get("sub")
  if sub is None:
    raise ValueError("Invalid token subject")
  if expected_scope is not None and payload.get("scope") != expected_scope:
    raise ValueError("Invalid token scope")
  return str(sub)

