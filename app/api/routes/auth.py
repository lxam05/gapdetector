from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.auth import (
  ForgotPasswordRequest,
  LoginRequest,
  RefreshRequest,
  RegisterRequest,
  ResetPasswordRequest,
  TokenPair,
)
from app.schemas.user import UserOut
from app.services import auth as auth_service


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut)
async def register(
  payload: RegisterRequest,
  db: AsyncSession = Depends(get_db),
) -> UserOut:
  user = await auth_service.register_user(db, payload)
  return UserOut.from_orm(user)


@router.get("/verify-email")
async def verify_email(
  token: str,
  db: AsyncSession = Depends(get_db),
) -> dict:
  await auth_service.verify_email_token(db, token)
  return {"detail": "Email verified. You can now log in."}


@router.post("/login", response_model=TokenPair)
async def login(
  payload: LoginRequest,
  db: AsyncSession = Depends(get_db),
) -> TokenPair:
  tokens = await auth_service.login_user(db, payload)
  return TokenPair(**tokens)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest) -> TokenPair:
  tokens = auth_service.refresh_tokens(payload.refresh_token)
  return TokenPair(**tokens)


@router.post("/forgot-password")
async def forgot_password(
  payload: ForgotPasswordRequest,
  db: AsyncSession = Depends(get_db),
) -> dict:
  # Avoid user enumeration by returning generic message.
  await auth_service.request_password_reset(db, payload.email)
  return {"detail": "If that email exists, a reset link has been sent."}


@router.post("/reset-password")
async def reset_password(
  payload: ResetPasswordRequest,
  db: AsyncSession = Depends(get_db),
) -> dict:
  await auth_service.reset_password(db, payload.token, payload.new_password)
  return {"detail": "Password has been reset."}


# Google OAuth placeholders – these would typically redirect to Google and back.

@router.get("/google/login")
async def google_login(request: Request) -> RedirectResponse:
  # In a real implementation, redirect to Google's OAuth endpoint with client_id, scopes, redirect_uri, state, etc.
  raise HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    detail="Google OAuth login not yet implemented.",
  )


@router.get("/google/callback")
async def google_callback(request: Request) -> RedirectResponse:
  # Exchange code for tokens, fetch userinfo, then create/link user and return JWTs or redirect.
  raise HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    detail="Google OAuth callback not yet implemented.",
  )

