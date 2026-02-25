from sqlalchemy.orm import declarative_base

Base = declarative_base()

# Import models here for Alembic's autogenerate to see them.
from app.models.user import User  # noqa: F401

