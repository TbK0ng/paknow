"""Database engine, session and declarative base."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from paknow.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.paknow_db_url,
    pool_pre_ping=True,
    future=True,
    connect_args={"check_same_thread": False}
    if settings.paknow_db_url.startswith("sqlite")
    else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Session:
    return SessionLocal()


def init_db() -> None:
    """Create all tables (dev bootstrap; Alembic migrations for production)."""
    from paknow.db import models  # noqa: F401  register mapped tables

    Base.metadata.create_all(engine)
