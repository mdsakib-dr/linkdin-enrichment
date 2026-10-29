"""SQLAlchemy ORM models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Unique business key — used for upsert on re-runs.
    linkedin_url: Mapped[str] = mapped_column(
        String(512), unique=True, nullable=False
    )

    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    mobile_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Full enrichment response for later re-processing.
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    search_query: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_leads_linkedin_url", "linkedin_url", unique=True),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Lead id={self.id} url={self.linkedin_url!r} name={self.full_name!r}>"
