"""Deterministic cleaning + validation of raw campaign rows.

Philosophy: code decides what is *valid*; the LLM only does judgement work
(canonical channel, objective, score). Three outcomes per row:
  - reject: unusable (junk/test rows, impossible values, duplicate ids)
  - accept with data_flags: usable but with caveats the score must reflect
  - accept clean
"""

import re
from dataclasses import dataclass, field

METRIC_FIELDS = ("spend", "impressions", "clicks", "conversions", "revenue")
INT_FIELDS = {"impressions", "clicks", "conversions"}
# Narrow on purpose: a bare "test" would wrongly reject real A/B-test campaigns.
JUNK_PATTERNS = re.compile(r"\b(do not use|draft row|asdf)\b", re.I)


class ParseError(ValueError):
    pass


def parse_number(value, field_name: str) -> float | None:
    """Parse a metric. None stays None (unknown != 0). Raises ParseError on garbage."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ParseError(f"{field_name}: boolean is not a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("₹", "")
        if s == "" or s.upper() in {"N/A", "NA", "NULL", "NONE", "-"}:
            return None
        try:
            return float(s)
        except ValueError:
            raise ParseError(f"{field_name}: non-numeric value {value!r}") from None
    raise ParseError(f"{field_name}: unsupported type {type(value).__name__}")


@dataclass
class CleanCampaign:
    source_id: str
    name: str | None
    description: str | None
    raw_channel: str | None
    spend: float | None
    impressions: int | None
    clicks: int | None
    conversions: int | None
    revenue: float | None
    roas: float | None = None
    cpa: float | None = None
    ctr: float | None = None
    data_flags: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    campaign: CleanCampaign | None
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.campaign is not None


def _ratio(num, den):
    if num is None or den is None or den == 0:
        return None
    return round(num / den, 5)


def validate_row(raw: dict) -> ValidationResult:
    errors: list[str] = []
    flags: list[str] = []

    if not isinstance(raw, dict):
        return ValidationResult(None, ["row is not an object"])

    source_id = str(raw.get("id") or "").strip()
    if not source_id:
        errors.append("missing id")

    name = (raw.get("name") or "").strip() or None
    description = (raw.get("description") or "").strip() or None
    raw_channel = (raw.get("channel") or "").strip() or None

    text = f"{name or ''} {description or ''}"
    if JUNK_PATTERNS.search(text):
        errors.append("looks like a test/draft row (name/description)")

    metrics: dict[str, float | None] = {}
    for f in METRIC_FIELDS:
        try:
            v = parse_number(raw.get(f), f)
        except ParseError as e:
            errors.append(str(e))
            continue
        if v is not None and v < 0:
            errors.append(f"{f}: negative value {v:g}")
            continue
        if v is not None and f in INT_FIELDS:
            v = int(v)
        metrics[f] = v

    if errors:
        return ValidationResult(None, errors)

    # accepted — now flag caveats
    if name is None:
        flags.append("missing_name")
    if not raw_channel or set(raw_channel) <= {"?"}:
        flags.append("unknown_raw_channel")
    if all(metrics.get(f) is None for f in ("impressions", "clicks", "conversions", "revenue")):
        flags.append("no_performance_data")
    else:
        for f in METRIC_FIELDS:
            if metrics.get(f) is None:
                flags.append(f"missing_{f}")
    if metrics.get("spend") == 0:
        flags.append("zero_spend")
    imp, clk, conv = metrics.get("impressions"), metrics.get("clicks"), metrics.get("conversions")
    if imp is not None and clk is not None and clk > imp:
        flags.append("clicks_exceed_impressions")
    if clk is not None and conv is not None and conv > clk:
        flags.append("conversions_exceed_clicks")
    if isinstance(raw.get("spend"), str) or isinstance(raw.get("revenue"), str):
        flags.append("coerced_string_metric")

    spend, revenue = metrics.get("spend"), metrics.get("revenue")
    c = CleanCampaign(
        source_id=source_id,
        name=name,
        description=description,
        raw_channel=raw_channel,
        **metrics,
        roas=_ratio(revenue, spend),
        cpa=_ratio(spend, conv),
        ctr=_ratio(clk, imp),
        data_flags=flags,
    )
    return ValidationResult(c, [])
