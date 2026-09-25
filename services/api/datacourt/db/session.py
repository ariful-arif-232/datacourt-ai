from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from datacourt.config import get_settings


def _json_default(o: object) -> object:
    import uuid as _uuid
    from datetime import date, datetime

    import numpy as np

    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (_uuid.UUID,)):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def json_serializer(obj: object) -> str:
    return json.dumps(obj, default=_json_default)


def is_pooled_url(url: str) -> bool:
    """Neon's PgBouncer endpoint ("-pooler" host) runs in transaction mode."""
    import os

    return "-pooler." in url or os.environ.get("DATABASE_POOLED", "").lower() in ("1", "true", "yes")


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    connect_args: dict = {"connect_timeout": 15}
    if is_pooled_url(settings.database_url):
        # Server-side prepared statements do not survive transaction pooling.
        connect_args["prepare_threshold"] = None
    serverless = settings.on_vercel
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=2 if serverless else 5,
        max_overflow=3 if serverless else 5,
        pool_recycle=240 if serverless else 300,
        future=True,
        json_serializer=json_serializer,
        connect_args=connect_args,
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def new_session() -> Session:
    return _session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for workers/scripts: commit on success, rollback on error."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()
