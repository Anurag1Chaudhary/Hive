from app.enrichment import Channel, enrich, rule_channel
from app.validation import CleanCampaign

GOOD = {"channel": "meta", "objective": "conversion", "health_score": 72,
        "rationale": "ROAS of 3.5 is solid for a retargeting campaign."}


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append(list(messages))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def camp(**kw):
    base = dict(source_id="x", name="n", description="d", raw_channel="fb", spend=100.0,
                impressions=1000, clicks=10, conversions=2, revenue=350.0, roas=3.5)
    base.update(kw)
    return CleanCampaign(**base)


def test_valid_first_try():
    r = enrich(camp(), FakeClient([GOOD]))
    assert r.status == "ok" and r.attempts == 1 and r.eval_flags == []


def test_retries_on_out_of_range_then_succeeds_and_feeds_error_back():
    client = FakeClient([{**GOOD, "health_score": 150}, GOOD])
    r = enrich(camp(), client)
    assert r.status == "ok" and r.attempts == 2
    assert "invalid" in client.calls[1][-1]["content"]


def test_falls_back_after_repeated_garbage():
    r = enrich(camp(), FakeClient([{"channel": "tiktok"}, ValueError("no tool call"), RuntimeError("503")]),
               max_retries=2)
    assert r.status == "fallback" and r.channel == Channel.meta and len(r.errors) == 3


def test_eval_flags_channel_disagreement():
    r = enrich(camp(raw_channel="yt"), FakeClient([GOOD]))
    assert any("channel_disagreement" in f for f in r.eval_flags)


def test_rule_channel_variants():
    assert rule_channel("Facebook Ads") == Channel.meta
    assert rule_channel("IG") == Channel.meta
    assert rule_channel("adwords") == Channel.google_ads
    assert rule_channel("Klaviyo email") == Channel.email
    assert rule_channel("???") is None
