from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Campaign(Base):
    """One enriched campaign. Upserted on source_id so re-ingest is idempotent."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    raw_channel: Mapped[str | None] = mapped_column(String(128))

    # cleaned metrics (NULL = unknown, never coerced to 0)
    spend: Mapped[float | None] = mapped_column(Numeric(14, 2))
    impressions: Mapped[int | None] = mapped_column(Integer)
    clicks: Mapped[int | None] = mapped_column(Integer)
    conversions: Mapped[int | None] = mapped_column(Integer)
    revenue: Mapped[float | None] = mapped_column(Numeric(14, 2))

    # derived in code, not by the LLM
    roas: Mapped[float | None] = mapped_column(Numeric(10, 3))
    cpa: Mapped[float | None] = mapped_column(Numeric(12, 2))
    ctr: Mapped[float | None] = mapped_column(Numeric(8, 5))

    # LLM enrichment
    channel: Mapped[str | None] = mapped_column(String(32), index=True)
    objective: Mapped[str | None] = mapped_column(String(32), index=True)
    health_score: Mapped[int | None] = mapped_column(Integer, index=True)
    rationale: Mapped[str | None] = mapped_column(Text)
    enrichment_status: Mapped[str] = mapped_column(String(16))  # ok | fallback
    data_flags: Mapped[list] = mapped_column(JsonType, default=list)
    eval_flags: Mapped[list] = mapped_column(JsonType, default=list)
    llm_model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(16))

    raw: Mapped[dict] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    total: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    rows: Mapped[list["IngestRowResult"]] = relationship(back_populates="run")


class IngestRowResult(Base):
    """Audit trail: what happened to every input row and why."""

    __tablename__ = "ingest_row_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("ingest_runs.id"), index=True)
    row_index: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # ok | fallback | rejected | error
    reasons: Mapped[list] = mapped_column(JsonType, default=list)
    run: Mapped[IngestRun] = relationship(back_populates="rows")
