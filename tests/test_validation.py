import json
from pathlib import Path

import pytest

from app.validation import ParseError, parse_number, validate_row

RAW = json.loads((Path(__file__).parent.parent / "data/campaigns_raw.json").read_text())["campaigns"]
BY_ID = {}
for r in RAW:
    BY_ID.setdefault(r["id"], r)


@pytest.mark.parametrize(
    "value,expected",
    [(185000, 185000.0), ("42,500", 42500.0), (" 1,200.5 ", 1200.5), (None, None), ("N/A", None), ("", None)],
)
def test_parse_number_ok(value, expected):
    assert parse_number(value, "spend") == expected


@pytest.mark.parametrize("value", ["lots", "many", True, [1]])
def test_parse_number_garbage(value):
    with pytest.raises(ParseError):
        parse_number(value, "x")


def test_string_spend_is_coerced_and_flagged():
    res = validate_row(BY_ID["cmp_002"])
    assert res.ok and res.campaign.spend == 42500
    assert "coerced_string_metric" in res.campaign.data_flags


def test_junk_row_rejected():
    res = validate_row(BY_ID["cmp_013"])
    assert not res.ok
    assert any("test/draft" in e for e in res.errors)
    assert any("negative" in e for e in res.errors)


def test_negative_spend_rejected():
    res = validate_row(BY_ID["cmp_014"])
    assert not res.ok and any("spend: negative" in e for e in res.errors)


def test_null_impressions_kept_as_null_not_zero():
    c = validate_row(BY_ID["cmp_009"]).campaign
    assert c.impressions is None and c.ctr is None
    assert "missing_impressions" in c.data_flags


def test_untracked_influencer_flagged():
    c = validate_row(BY_ID["cmp_012"]).campaign
    assert "no_performance_data" in c.data_flags and c.roas is None


def test_blank_name_flagged_not_rejected():
    c = validate_row(BY_ID["cmp_011"]).campaign
    assert c.name is None and "missing_name" in c.data_flags


def test_zero_spend_has_no_roas():
    c = validate_row(BY_ID["cmp_006"]).campaign
    assert c.roas is None and "zero_spend" in c.data_flags


def test_derived_metrics():
    c = validate_row(BY_ID["cmp_004"]).campaign
    assert c.roas == pytest.approx(1965000 / 28000, rel=1e-4)
    assert c.ctr == pytest.approx(11400 / 92000, rel=1e-3)
