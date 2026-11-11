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
        # Hand-written flat schema: Pydantic's $defs/$ref output confused gpt-oss (it sent strings, not objects).
        "parameters": {
            "type": "object",
            "properties": {
                "observations": {
                    "type": "array", "minItems": 2, "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "detail": {"type": "string", "description": "2-3 sentences citing numbers"},
                            "campaigns": {"type": "array", "items": {"type": "string"},
                                          "description": "campaign ids referenced, e.g. cmp_003"},
                        },
                        "required": ["title", "detail", "campaigns"],
                    },
                },
            },
            "required": ["observations"],
        },
    },
}

INSIGHTS_PROMPT = """You are a performance-marketing lead reviewing a D2C skincare brand's portfolio (INR).
Using ONLY the aggregates and campaign rows provided, call record_insights with 2-3 observations,
prioritising where budget is being wasted or should be reallocated. Cite numbers and campaign ids.
Do not invent data. null revenue/conversions means UNTRACKED (unknown), never zero: say the impact can't be
measured rather than claiming it produced nothing."""


def aggregate(db: Session) -> dict:
    """NULL revenue means *untracked*, not zero. ROAS is computed only over spend whose revenue is
    tracked, and untracked spend is reported separately, so neither the API nor the LLM sees a fake ₹0."""
    tracked = Campaign.revenue.is_not(None)
    by_channel = db.execute(
        select(Campaign.channel, func.count(), func.sum(Campaign.spend),
               func.sum(Campaign.spend).filter(tracked), func.sum(Campaign.revenue),
               func.avg(Campaign.health_score))
        .group_by(Campaign.channel).order_by(func.sum(Campaign.spend).desc().nulls_last())
    ).all()
    channels = []
    for ch, n, sp, tracked_sp, rev, avg in by_channel:
        sp, tracked_sp = float(sp or 0), float(tracked_sp or 0)
        channels.append({
            "channel": ch, "campaigns": n, "spend": sp,
            "untracked_spend": sp - tracked_sp,
            "revenue": float(rev) if rev is not None else None,
            "roas": round(float(rev) / tracked_sp, 2) if rev is not None and tracked_sp else None,
            "avg_health_score": round(float(avg), 1) if avg is not None else None,
        })
    rows = db.scalars(select(Campaign).order_by(Campaign.spend.desc().nulls_last())).all()
    campaigns = [{"id": c.source_id, "name": c.name, "channel": c.channel, "objective": c.objective,
                  "spend": float(c.spend) if c.spend is not None else None,
                  "revenue": float(c.revenue) if c.revenue is not None else None,
                  "roas": float(c.roas) if c.roas is not None else None,
                  "cpa": float(c.cpa) if c.cpa is not None else None,
                  "health_score": c.health_score, "flags": c.data_flags} for c in rows]
    total_spend = sum(ch["spend"] for ch in channels)
    untracked = sum(ch["untracked_spend"] for ch in channels)
    total_rev = sum(ch["revenue"] or 0 for ch in channels)
    tracked_spend = total_spend - untracked
    return {"totals": {"spend": total_spend, "untracked_spend": untracked, "tracked_revenue": total_rev,
                       "blended_roas_tracked": round(total_rev / tracked_spend, 2) if tracked_spend else None},
            "by_channel": channels, "campaigns": campaigns}


def generate_insights(db: Session, groq_client) -> dict:
    agg = aggregate(db)
    result = {"aggregates": agg, "observations": None, "llm_error": None}
    if groq_client is None:
        result["llm_error"] = "LLM not configured"
        return result
    for attempt in range(2):  # one retry: Groq returns 400 tool_use_failed on malformed tool calls
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
            result["llm_error"] = None
            break
        except (ValidationError, ValueError, TypeError, IndexError) as e:
            result["llm_error"] = f"invalid model output: {str(e)[:300]}"
        except Exception as e:
            result["llm_error"] = f"llm call failed: {type(e).__name__}: {str(e)[:200]}"
    return result
