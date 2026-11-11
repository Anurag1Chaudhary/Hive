import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base, engine, get_session
from app.enrichment import Channel, GroqClient, Objective
from app.ingest import load_raw_file, run_ingest
from app.insights import generate_insights
from app.models import Campaign, IngestRun
from app.schemas import CampaignOut, IngestRequest, IngestSummary, RowResult

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)  # fine for an exercise; Alembic migrations in real life
    yield


app = FastAPI(title="Hive — campaign intelligence", lifespan=lifespan)


def get_llm_client():
    try:
        return GroqClient()
    except RuntimeError:
        return None


@app.post("/campaigns/ingest", response_model=IngestSummary)
def ingest(body: IngestRequest | None = None, db: Session = Depends(get_session),
           client=Depends(get_llm_client)):
    body = body or IngestRequest()
    if client is None:
        raise HTTPException(503, "LLM not configured: set GROQ_API_KEY")
    try:
        rows = body.campaigns if body.campaigns is not None else load_raw_file()
    except (OSError, ValueError) as e:
        raise HTTPException(400, f"could not load raw campaigns: {e}")
    return run_ingest(db, rows, client, client.model, force=body.force)


@app.get("/campaigns", response_model=list[CampaignOut])
def list_campaigns(
    channel: Channel | None = None,
    objective: Objective | None = None,
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
    status: str | None = Query(None, pattern="^(ok|fallback)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_session),
):
    q = select(Campaign)
    if channel:
        q = q.where(Campaign.channel == channel.value)
    if objective:
        q = q.where(Campaign.objective == objective.value)
    if min_score is not None:
        q = q.where(Campaign.health_score >= min_score)
    if max_score is not None:
        q = q.where(Campaign.health_score <= max_score)
    if status:
        q = q.where(Campaign.enrichment_status == status)
    q = q.order_by(Campaign.health_score.desc().nulls_last(), Campaign.source_id).limit(limit).offset(offset)
    return db.scalars(q).all()


@app.get("/campaigns/insights")
def insights(db: Session = Depends(get_session), client=Depends(get_llm_client)):
    """Declared before /campaigns/{source_id} so 'insights' isn't treated as an id.
    Always returns SQL aggregates; observations are null with llm_error if the LLM fails."""
    return generate_insights(db, client)


@app.get("/campaigns/{source_id}", response_model=CampaignOut)
def get_campaign(source_id: str, db: Session = Depends(get_session)):
    c = db.scalar(select(Campaign).where(Campaign.source_id == source_id))
    if not c:
        raise HTTPException(404, f"campaign {source_id} not found")
    return c


@app.get("/ingest/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_session)):
    run = db.get(IngestRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return {"run_id": run.id, "started_at": run.started_at, "total": run.total,
            "succeeded": run.succeeded, "failed": run.failed,
            "rows": [RowResult(row_index=r.row_index, source_id=r.source_id, status=r.status,
                               reasons=r.reasons) for r in sorted(run.rows, key=lambda r: r.row_index)]}


@app.get("/health")
def health():
    return {"ok": True, "llm_configured": bool(settings.groq_api_key), "model": settings.groq_model}
