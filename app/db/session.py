from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


def _make_async_database_url(url: str) -> str:
  """Ensure DATABASE_URL is in asyncpg form for SQLAlchemy async engine."""
  if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql+asyncpg://", 1)
  elif url.startswith("postgresql://") and "+asyncpg" not in url:
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
  return url


ASYNC_DATABASE_URL = _make_async_database_url(str(settings.DATABASE_URL))

engine = create_async_engine(ASYNC_DATABASE_URL, echo=False, future=True)

AsyncSessionLocal = async_sessionmaker(
  bind=engine,
  expire_on_commit=False,
  class_=AsyncSession,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
  async with AsyncSessionLocal() as session:
    yield session

