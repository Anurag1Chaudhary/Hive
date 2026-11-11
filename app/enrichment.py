"""LLM enrichment via forced tool calling, validated by Pydantic.

Flow per campaign:
  1. Call the model with tool_choice forced to `record_enrichment`.
  2. Validate the tool arguments against EnrichmentOutput (enums + ranges).
  3. On a malformed / invalid response: retry, feeding the validation error
     back to the model so it can self-correct.
  4. After max retries: deterministic fallback (rule-based channel, no score)
     and status="fallback" — the batch never fails because of one row.
"""

import json
import logging
from dataclasses import asdict
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.validation import CleanCampaign

log = logging.getLogger(__name__)
PROMPT_VERSION = "v1"


class Channel(str, Enum):
    meta = "meta"            # Facebook + Instagram
    google_ads = "google_ads"  # Search, PMax, Shopping (not YouTube)
    youtube = "youtube"
    email = "email"
    sms = "sms"
    influencer = "influencer"
    other = "other"


class Objective(str, Enum):
    awareness = "awareness"
    consideration = "consideration"
    conversion = "conversion"
    retention = "retention"


class EnrichmentOutput(BaseModel):
    channel: Channel
    objective: Objective
    health_score: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=10, max_length=300)


TOOL = {
    "type": "function",
    "function": {
        "name": "record_enrichment",
        "description": "Record the normalized channel, inferred objective and health score for one campaign.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": [c.value for c in Channel],
                            "description": "Canonical channel. Instagram/Facebook => meta. Search/PMax/AdWords => google_ads. YouTube is separate."},
                "objective": {"type": "string", "enum": [o.value for o in Objective],
                              "description": "Funnel objective inferred from name + description (not from performance)."},
                "health_score": {"type": "integer", "minimum": 0, "maximum": 100},
                "rationale": {"type": "string", "description": "ONE sentence citing the metric(s) that drove the score."},
            },
            "required": ["channel", "objective", "health_score", "rationale"],
            "additionalProperties": False,
        },
    },
}

SYSTEM_PROMPT = """You are a performance-marketing analyst for a D2C skincare brand (currency INR).
For ONE campaign, call `record_enrichment` exactly once. Do not reply with text.

channel: map the raw channel label (often messy: "fb", "IG", "adwords", "yt", "Klaviyo email") to the enum.
objective: infer from name + description, NOT from results.
  awareness = reach/video/brand lift; consideration = engagement/traffic/UGC;
  conversion = acquisition/purchase-optimized/search/retargeting; retention = existing customers (winback, loyalty, flows to past buyers).

health_score (0-100): judge the campaign AGAINST ITS OWN OBJECTIVE using the precomputed metrics
(do not recompute them; they are correct).
  - conversion/retention: ROAS >= 6 excellent (80-100), 3-6 good (60-80), 1.5-3 weak (35-60), < 1.5 poor (0-35).
  - awareness: judge on CPM and reach; a low ROAS is expected and is NOT by itself a failure.
  - consideration: judge on CTR and cost per click, with ROAS as a secondary signal.
  - If data_flags contains "no_performance_data", performance cannot be verified: score <= 30 and say so.
  - zero spend (owned channels like email) => ROAS is undefined; judge on conversions and revenue.
rationale: one sentence, cite the specific number(s)."""


def build_user_message(c: CleanCampaign) -> str:
    d = asdict(c)
    cpm = round(c.spend / c.impressions * 1000, 2) if c.spend and c.impressions else None
    payload = {
        "name": d["name"], "description": d["description"], "raw_channel": d["raw_channel"],
        "metrics": {k: d[k] for k in ("spend", "impressions", "clicks", "conversions", "revenue")},
        "derived": {"roas": c.roas, "cpa": c.cpa, "ctr": c.ctr, "cpm": cpm},
        "data_flags": c.data_flags,
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------- deterministic fallback / eval reference ----------

_CHANNEL_RULES: list[tuple[tuple[str, ...], Channel]] = [
    (("youtube", "yt"), Channel.youtube),
    (("fb", "facebook", "meta", "ig", "instagram"), Channel.meta),
    (("google", "adwords", "pmax", "search"), Channel.google_ads),
    (("email", "klaviyo", "mailchimp"), Channel.email),
    (("sms", "whatsapp"), Channel.sms),
    (("influencer", "creator"), Channel.influencer),
]


def rule_channel(raw_channel: str | None) -> Channel | None:
    """Deterministic channel map. None = no rule matched (unknown)."""
    if not raw_channel:
        return None
    tokens = raw_channel.lower().replace("-", " ").split()
    for keys, ch in _CHANNEL_RULES:
        if any(t in keys for t in tokens):
            return ch
    return None


def self_check(c: CleanCampaign, out: EnrichmentOutput) -> list[str]:
    """Lightweight eval: compare model output against deterministic rules."""
    flags = []
    expected = rule_channel(c.raw_channel)
    if expected and expected != out.channel:
        flags.append(f"channel_disagreement: rule={expected.value} llm={out.channel.value}")
    if "no_performance_data" in c.data_flags and out.health_score > 30:
        flags.append(f"score_too_high_for_untracked: {out.health_score}")
    if (out.objective in (Objective.conversion, Objective.retention) and c.roas is not None
            and c.roas < 1.5 and out.health_score > 60):
        flags.append(f"score_inconsistent_with_roas: roas={c.roas} score={out.health_score}")
    return flags


# ---------- LLM call ----------

class ChatClient(Protocol):
    def complete(self, messages: list[dict]) -> dict: ...


class GroqClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from groq import Groq

        key = api_key or settings.groq_api_key
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set")
        self.model = model or settings.groq_model
        self._client = Groq(api_key=key, max_retries=2, timeout=30)

    def complete(self, messages: list[dict]) -> dict:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=[TOOL],
            tool_choice={"type": "function", "function": {"name": "record_enrichment"}},
            temperature=0,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            raise ValueError(f"model returned no tool call (content={msg.content!r:.200})")
        return json.loads(msg.tool_calls[0].function.arguments)


class EnrichmentResult(BaseModel):
    status: str  # ok | fallback
    output: EnrichmentOutput | None = None
    channel: Channel | None = None
    attempts: int = 0
    errors: list[str] = []
    eval_flags: list[str] = []


def enrich(c: CleanCampaign, client: ChatClient, max_retries: int | None = None) -> EnrichmentResult:
    max_retries = settings.llm_max_retries if max_retries is None else max_retries
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(c)},
    ]
    errors: list[str] = []
    for attempt in range(1, max_retries + 2):
        try:
            args = client.complete(messages)
            out = EnrichmentOutput.model_validate(args)
            return EnrichmentResult(status="ok", output=out, channel=out.channel,
                                    attempts=attempt, errors=errors, eval_flags=self_check(c, out))
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            # Malformed output: tell the model exactly what was wrong and retry.
            err = f"attempt {attempt}: invalid model output: {str(e)[:300]}"
            errors.append(err)
            log.warning("%s %s", c.source_id, err)
            messages.append({"role": "user",
                             "content": f"Your previous tool call was invalid: {str(e)[:300]}. "
                                        "Call record_enrichment again with valid arguments."})
        except Exception as e:  # network / rate limit / API errors
            errors.append(f"attempt {attempt}: llm call failed: {type(e).__name__}: {str(e)[:200]}")
            log.warning("%s llm error: %s", c.source_id, e)
    return EnrichmentResult(status="fallback", channel=rule_channel(c.raw_channel) or Channel.other,
                            attempts=max_retries + 1, errors=errors)
