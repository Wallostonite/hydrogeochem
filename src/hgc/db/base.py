from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_engine(url: str, pool_size: int = 10, max_overflow: int = 10):
    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,  # survives Postgres failover and idle connection reaping
        pool_recycle=1800,
        future=True,
    )


def build_session_factory(url: str, **kwargs) -> sessionmaker[Session]:
    return sessionmaker(bind=build_engine(url, **kwargs), expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
