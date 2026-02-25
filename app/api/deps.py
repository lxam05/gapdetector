from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import User


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(
  token: str = Depends(oauth2_scheme),
  db: AsyncSession = Depends(get_db),
) -> User:
  credentials_exception = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
  )
  try:
    payload = decode_token(token)
    user_id: str = payload.get("sub")
    scope = payload.get("scope")
    if user_id is None or scope != "access":
      raise credentials_exception
  except JWTError:
    raise credentials_exception

  user = await db.get(User, user_id)
  if not user or not user.is_active:
    raise credentials_exception
  return user

