"""Portfolio insights: SQL does the aggregation, the LLM only interprets."""
import json

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Campaign


class Observation(BaseModel):
    title: str = Field(max_length=120)
    detail: str = Field(max_length=400)
    campaigns: list[str] = Field(default_factory=list, description="source_ids referenced")


class InsightsOutput(BaseModel):
    observations: list[Observation] = Field(min_length=2, max_length=3)


INSIGHTS_TOOL = {
    "type": "function",
    "function": {
        "name": "record_insights",
        "description": "Record 2-3 portfolio-level observations.",
        "parameters": InsightsOutput.model_json_schema(),
    },
}

INSIGHTS_PROMPT = """You are a performance-marketing lead reviewing a D2C skincare brand's portfolio (INR).
Using ONLY the aggregates and campaign rows provided, call record_insights with 2-3 observations,
prioritising where budget is being wasted or should be reallocated. Cite numbers and campaign ids.
Do not invent data; campaigns with null metrics are untracked, call that out rather than guessing."""


def aggregate(db: Session) -> dict:
    by_channel = db.execute(
        select(Campaign.channel, func.count(), func.sum(Campaign.spend), func.sum(Campaign.revenue),
               func.avg(Campaign.health_score))
        .group_by(Campaign.channel).order_by(func.sum(Campaign.spend).desc().nulls_last())
    ).all()
    channels = [{
        "channel": ch, "campaigns": n, "spend": float(sp or 0), "revenue": float(rev or 0),
        "roas": round(float(rev) / float(sp), 2) if sp and rev else None,
        "avg_health_score": round(float(avg), 1) if avg is not None else None,
    } for ch, n, sp, rev, avg in by_channel]
    rows = db.scalars(select(Campaign).order_by(Campaign.spend.desc().nulls_last())).all()
    campaigns = [{"id": c.source_id, "name": c.name, "channel": c.channel, "objective": c.objective,
                  "spend": float(c.spend) if c.spend is not None else None,
                  "roas": float(c.roas) if c.roas is not None else None,
                  "cpa": float(c.cpa) if c.cpa is not None else None,
                  "health_score": c.health_score, "flags": c.data_flags} for c in rows]
    total_spend = sum(ch["spend"] for ch in channels)
    total_rev = sum(ch["revenue"] for ch in channels)
    return {"totals": {"spend": total_spend, "revenue": total_rev,
                       "blended_roas": round(total_rev / total_spend, 2) if total_spend else None},
            "by_channel": channels, "campaigns": campaigns}


def generate_insights(db: Session, groq_client) -> dict:
    agg = aggregate(db)
    result = {"aggregates": agg, "observations": None, "llm_error": None}
    if groq_client is None:
        result["llm_error"] = "LLM not configured"
        return result
    try:
        resp = groq_client._client.chat.completions.create(
            model=groq_client.model, temperature=0,
            messages=[{"role": "system", "content": INSIGHTS_PROMPT},
                      {"role": "user", "content": json.dumps(agg, ensure_ascii=False)}],
            tools=[INSIGHTS_TOOL],
            tool_choice={"type": "function", "function": {"name": "record_insights"}},
        )
        args = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
        result["observations"] = InsightsOutput.model_validate(args).model_dump()["observations"]
    except (ValidationError, ValueError, TypeError, IndexError) as e:
        result["llm_error"] = f"invalid model output: {str(e)[:300]}"
    except Exception as e:
        result["llm_error"] = f"llm call failed: {type(e).__name__}: {str(e)[:200]}"
    return result
