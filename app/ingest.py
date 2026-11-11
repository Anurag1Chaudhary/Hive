import json
import logging
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.enrichment import PROMPT_VERSION, ChatClient, enrich
from app.models import Campaign, IngestRowResult, IngestRun
from app.schemas import IngestSummary, RowResult
from app.validation import validate_row

log = logging.getLogger(__name__)


def load_raw_file(path: str | None = None) -> list:
    data = json.loads(Path(path or settings.raw_data_path).read_text())
    rows = data.get("campaigns") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("raw file must contain a 'campaigns' list")
    return rows


def _upsert(db: Session, values: dict) -> None:
    stmt = insert(Campaign).values(**values)
    update_cols = {k: stmt.excluded[k] for k in values if k != "source_id"}
    db.execute(stmt.on_conflict_do_update(index_elements=["source_id"], set_=update_cols))


def run_ingest(db: Session, rows: list, client: ChatClient | None, model_name: str | None,
               force: bool = False) -> IngestSummary:
    run = IngestRun(total=len(rows))
    db.add(run)
    db.flush()

    results: list[RowResult] = []
    seen_ids: set[str] = set()

    for i, raw in enumerate(rows):
        source_id = str(raw.get("id")) if isinstance(raw, dict) and raw.get("id") else None
        try:
            v = validate_row(raw)
            if not v.ok:
                results.append(RowResult(row_index=i, source_id=source_id, status="rejected", reasons=v.errors))
                continue
            c = v.campaign
            if c.source_id in seen_ids:
                # Policy: first occurrence wins; later rows with the same id are rejected, not merged.
                results.append(RowResult(row_index=i, source_id=c.source_id, status="rejected",
                                         reasons=["duplicate id in batch (first occurrence kept)"]))
                continue
            seen_ids.add(c.source_id)

            existing = db.scalar(select(Campaign).where(Campaign.source_id == c.source_id))
            if (not force and existing is not None and existing.raw == raw
                    and existing.enrichment_status == "ok"):
                results.append(RowResult(row_index=i, source_id=c.source_id, status="skipped_unchanged",
                                         reasons=["identical to stored row; LLM not re-called"]))
                continue

            if client is None:
                raise RuntimeError("no LLM client configured (set GROQ_API_KEY)")
            e = enrich(c, client)
            out = e.output
            _upsert(db, dict(
                source_id=c.source_id, name=c.name, description=c.description, raw_channel=c.raw_channel,
                spend=c.spend, impressions=c.impressions, clicks=c.clicks, conversions=c.conversions,
                revenue=c.revenue, roas=c.roas, cpa=c.cpa, ctr=c.ctr,
                channel=e.channel.value if e.channel else None,
                objective=out.objective.value if out else None,
                health_score=out.health_score if out else None,
                rationale=out.rationale if out else None,
                enrichment_status=e.status, data_flags=c.data_flags, eval_flags=e.eval_flags,
                llm_model=model_name, prompt_version=PROMPT_VERSION, raw=raw,
            ))
            db.commit()  # commit per row: one later failure can't roll back earlier successes
            reasons = c.data_flags + e.eval_flags + e.errors
            results.append(RowResult(row_index=i, source_id=c.source_id, status=e.status, reasons=reasons))
        except Exception as ex:
            db.rollback()
            log.exception("row %s failed", i)
            results.append(RowResult(row_index=i, source_id=source_id, status="error",
                                     reasons=[f"{type(ex).__name__}: {ex}"]))

    ok_statuses = {"ok", "fallback", "skipped_unchanged"}
    run = db.merge(run)
    run.succeeded = sum(r.status in ok_statuses for r in results)
    run.failed = len(results) - run.succeeded
    for r in results:
        db.add(IngestRowResult(run_id=run.id, row_index=r.row_index, source_id=r.source_id,
                               status=r.status, reasons=r.reasons))
    db.commit()

    return IngestSummary(run_id=run.id, total=run.total, succeeded=run.succeeded, failed=run.failed,
                         by_status=dict(Counter(r.status for r in results)), rows=results)
