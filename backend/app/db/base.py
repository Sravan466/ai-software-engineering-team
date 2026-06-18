"""SQLAlchemy engine, session factory, and FastAPI dependency.

Uses SQLite by default (file under ./data). Switch to Postgres by setting DATABASE_URL.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# Ensure the SQLite directory exists (sqlite:///./data/aiteam.db -> ./data).
if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.split("///", 1)[-1]
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def init_db() -> None:
    """Create tables. Import models so they register on the metadata."""
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
