"""Database engine/session setup and upsert logic."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterable, Iterator, List

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Lead

logger = logging.getLogger(__name__)

# Columns that should be refreshed when a conflicting row already exists.
# We deliberately do NOT overwrite created_at; updated_at is handled by onupdate.
_UPSERT_UPDATE_COLUMNS = (
    "full_name",
    "headline",
    "job_title",
    "company_name",
    "location",
    "email",
    "mobile_number",
    "raw_data",
    "search_query",
)


def make_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine with sane pool defaults."""
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        future=True,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on exception."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upsert_leads(session: Session, rows: Iterable[dict]) -> int:
    """Upsert lead rows using INSERT ... ON CONFLICT (linkedin_url) DO UPDATE.

    `rows` is an iterable of dicts keyed by Lead column names. Rows missing a
    `linkedin_url` are skipped. Returns the number of rows sent to the database.

    All rows are applied within the caller's transaction (one statement), so the
    caller controls commit/rollback via `session_scope`.
    """
    rows = [r for r in rows if r.get("linkedin_url")]
    if not rows:
        return 0

    # Deduplicate within the batch itself — ON CONFLICT cannot handle two rows
    # with the same conflict key in a single INSERT. Last one wins.
    deduped: dict[str, dict] = {}
    for r in rows:
        deduped[r["linkedin_url"]] = r
    rows = list(deduped.values())

    stmt = pg_insert(Lead).values(rows)
    update_set = {
        col: getattr(stmt.excluded, col) for col in _UPSERT_UPDATE_COLUMNS
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["linkedin_url"],
        set_=update_set,
    )
    session.execute(stmt)
    return len(rows)


def init_db(engine: Engine) -> None:
    """Create tables directly (for quick local setup / tests).

    Prefer Alembic migrations for real deployments.
    """
    from .models import Base

    Base.metadata.create_all(engine)
