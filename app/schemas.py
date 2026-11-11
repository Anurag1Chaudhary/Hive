from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_id: str
    name: str | None
    description: str | None
    raw_channel: str | None
    channel: str | None
    objective: str | None
    health_score: int | None
    rationale: str | None
    enrichment_status: str
    spend: float | None
    impressions: int | None
    clicks: int | None
    conversions: int | None
    revenue: float | None
    roas: float | None
    cpa: float | None
    ctr: float | None
    data_flags: list[str]
    eval_flags: list[str]
    llm_model: str | None
    prompt_version: str | None
    updated_at: datetime


class RowResult(BaseModel):
    row_index: int
    source_id: str | None
    status: str  # ok | fallback | skipped_unchanged | rejected | error
    reasons: list[str] = []


class IngestSummary(BaseModel):
    run_id: int
    total: int
    succeeded: int
    failed: int
    by_status: dict[str, int]
    rows: list[RowResult]


class IngestRequest(BaseModel):
    """Optional: post campaigns inline. If omitted, the configured raw file is read."""
    # list[Any], not list[dict]: a single malformed row must be rejected per-row, not 422 the batch
    campaigns: list[Any] | None = None
    force: bool = False  # re-run the LLM even if the row is unchanged
