"""End-to-end through the API against a real Postgres test DB, with a fake LLM."""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base, get_session
from app.enrichment import rule_channel

TEST_DB = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg://hive:hive@localhost:5442/hive_test")


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        import json
        payload = json.loads(messages[1]["content"])
        if payload["name"] and "Reels" in payload["name"]:
            return {"channel": "meta"}  # always malformed -> exercises fallback
        ch = rule_channel(payload["raw_channel"])
        return {"channel": ch.value if ch else "other", "objective": "conversion",
                "health_score": 70, "rationale": "Test rationale with a number 3.0."}


@pytest.fixture()
def client():
    try:
        engine = create_engine(TEST_DB)
        engine.connect().close()
    except Exception:
        pytest.skip("test Postgres not available")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)

    def _session():
        with TestSession() as s:
            yield s

    fake = FakeLLM()
    main.app.dependency_overrides[get_session] = _session
    main.app.dependency_overrides[main.get_llm_client] = lambda: fake
    with TestClient(main.app) as c:
        c.fake = fake
        c.engine = engine
        yield c
    main.app.dependency_overrides.clear()


def test_ingest_summary_and_idempotency(client):
    r = client.post("/campaigns/ingest")
    assert r.status_code == 200
    s = r.json()
    assert s["total"] == 15
    by_id = {}
    for row in s["rows"]:
        by_id.setdefault(row["source_id"], []).append(row["status"])
    assert by_id["cmp_013"] == ["rejected"]
    assert by_id["cmp_014"] == ["rejected"]
    assert by_id["cmp_008"] == ["ok", "rejected"]  # duplicate id
    assert by_id["cmp_007"] == ["fallback"]        # malformed LLM output

    with client.engine.connect() as conn:
        assert conn.execute(text("select count(*) from campaigns")).scalar() == 12

    calls_before = client.fake.calls
    s2 = client.post("/campaigns/ingest").json()
    with client.engine.connect() as conn:
        assert conn.execute(text("select count(*) from campaigns")).scalar() == 12  # no duplicates
    assert s2["by_status"]["skipped_unchanged"] == 11
    # only the fallback row is retried (3 attempts); unchanged ok rows don't hit the LLM
    assert client.fake.calls - calls_before == 3


def test_filters_and_get(client):
    client.post("/campaigns/ingest")
    meta = client.get("/campaigns", params={"channel": "meta"}).json()
    assert meta and all(c["channel"] == "meta" for c in meta)
    assert all(c["health_score"] >= 70 for c in client.get("/campaigns", params={"min_score": 70}).json())
    assert client.get("/campaigns/cmp_002").json()["spend"] == 42500
    assert client.get("/campaigns/nope").status_code == 404
    assert client.get("/campaigns", params={"channel": "tiktok"}).status_code == 422


def test_ingest_inline_bad_rows_dont_sink_batch(client):
    rows = [None, "garbage", {"id": "x1", "name": "ok", "channel": "fb", "spend": 10,
                              "impressions": 100, "clicks": 5, "conversions": 1, "revenue": 50}]
    s = client.post("/campaigns/ingest", json={"campaigns": rows}).json()
    assert [r["status"] for r in s["rows"]] == ["rejected", "rejected", "ok"]
