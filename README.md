# Hive — Campaign Intelligence Service

Ingests messy ad-ops campaign exports for **Aster & Oak**, cleans and validates them in code, enriches each
valid campaign with an LLM (canonical channel, objective, health score + rationale) using **forced tool calling**,
stores the results in **PostgreSQL**, and serves them through a **FastAPI** API.

## Run it

```bash
docker compose up -d --wait                 # Postgres 16 on localhost:5442
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                        # set GROQ_API_KEY (free key: console.groq.com)
.venv/bin/uvicorn app.main:app --reload

curl -X POST localhost:8000/campaigns/ingest | jq           # per-row summary
curl 'localhost:8000/campaigns?channel=meta&min_score=60' | jq
curl localhost:8000/campaigns/cmp_004 | jq
curl localhost:8000/campaigns/insights | jq                 # stretch
curl localhost:8000/ingest/runs/1 | jq                      # audit trail
```

Interactive docs are at `http://localhost:8000/docs`.

Tests: `docker compose exec db psql -U hive -c "CREATE DATABASE hive_test"` once, then `.venv/bin/pytest -q`.
The API tests use the real Postgres with a fake LLM, so they're deterministic and need no key.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/campaigns/ingest` | Body optional. `{"campaigns": [...]}` posts rows inline; otherwise `data/campaigns_raw.json` is read. `{"force": true}` re-runs the LLM on unchanged rows. |
| GET | `/campaigns` | Filters: `channel`, `objective`, `min_score`, `max_score`, `status` (ok/fallback), `limit`, `offset`. Sorted by score desc. |
| GET | `/campaigns/{source_id}` | 404 if missing |
| GET | `/campaigns/insights` | SQL aggregates + 2–3 LLM observations |
| GET | `/ingest/runs/{id}` | Stored per-row results for a past run |

## Pipeline & key design choices

```
raw row ─► validate/clean (code) ─► dedupe ─► LLM enrich (tool call) ─► Pydantic validate ─► self-check ─► upsert
             │ reject + reason                  │ retry w/ error fed back, then deterministic fallback
```

**1. Code decides validity; the LLM only does judgement.** Parsing numbers, rejecting junk and computing
ROAS/CPA/CTR are deterministic jobs, so they're done in code where they're testable and free. The LLM gets the
derived metrics precomputed and is told not to recompute them. That removes the most common LLM failure on
numeric data, which is bad arithmetic.

**2. Data cleaning decisions** (there's no single right answer, so these are mine):

| Row | Issue | Decision |
|---|---|---|
| cmp_002 | spend `"42,500"` | Parsed to 42500, flagged `coerced_string_metric` |
| cmp_009 | impressions `null` | Kept as **NULL, not 0**. CTR is unknown, not 0%. Flagged |
| cmp_011 | blank name | Accepted. The description is enough for the LLM. Flagged `missing_name` |
| cmp_012 | influencer, no tracking | Accepted and flagged `no_performance_data`. The prompt caps the score at ≤30, and the self-check verifies that |
| cmp_006 | spend 0 (owned email) | ROAS is undefined (NULL, not ∞). The prompt says to judge on conversions/revenue |
| cmp_013 | "DO NOT USE", `"lots"`, clicks −5 | **Rejected**, with every reason listed |
| cmp_014 | spend −1200 | **Rejected.** Negative spend is a data error, and I'd rather surface it than guess it's a refund/credit |
| cmp_008 ×2 | duplicate id | **First occurrence wins**, and the second is rejected with a reason. I don't merge or average because there's no way to know which row is correct, so it goes back to ad-ops |

The junk-row regex is deliberately narrow (`do not use|draft row|asdf`). A bare `test` would wrongly reject
real A/B-test campaigns.

**3. Schema.**
- `campaigns`: one row per `source_id` (unique). It has cleaned metrics, derived metrics, enrichment fields,
  `enrichment_status` (ok/fallback), `data_flags` and `eval_flags` (JSONB), `llm_model`, `prompt_version`, and
  the original `raw` row (JSONB) for lineage and re-processing.
- `ingest_runs` and `ingest_row_results`: an audit trail of what happened to every input row and why.
- Indexes are on `channel`, `objective` and `health_score`, which are the filter columns.
- Money is stored as `NUMERIC`, never float.

**4. Failure isolation.** Each row runs inside its own try/except and is **committed individually**, so
neither a bad row nor a DB error on one row can roll back the others. Non-object rows in an inline POST are
rejected per row (the body is typed `list[Any]`, not `list[dict]`, so one bad row can't 422 the whole batch).

**5. Idempotent re-ingest.** Rows are upserted with `INSERT … ON CONFLICT (source_id) DO UPDATE`. If the stored
raw row is identical and was enriched successfully, the LLM is **not called again** (`skipped_unchanged`). That
saves cost and keeps scores stable between runs. `fallback` rows are retried on the next ingest.

## AI & prompt choices

- **Model:** Groq `llama-3.3-70b-versatile` (free tier, OpenAI-compatible tool calling). It's configurable via
  `GROQ_MODEL`, and the key is only ever read from the env var.
- **Structured output via forced tool calling.** `tool_choice` is pinned to `record_enrichment`, and the
  parameters are a JSON schema with **enums** for `channel` and `objective`, plus `health_score` limited to
  0–100. The model can't answer in free text, and there's no regex parsing. Groq doesn't guarantee schema
  conformance for every model, so each response is **re-validated with Pydantic** (`EnrichmentOutput`).
- **Canonical channels:** `meta` (FB + IG), `google_ads` (Search/PMax), `youtube`, `email`, `sms`,
  `influencer`, `other`. YouTube is kept separate from Google Ads because it's bought and judged differently
  (awareness/video). I kept the set coarse so a deterministic rule can cross-check it.
- **Prompt design:**
  - The objective is inferred from **name + description, not results**. Otherwise a poorly performing
    conversion campaign gets relabelled as "awareness" to excuse it.
  - The health score is judged **against the campaign's own objective**, using an anchored ROAS rubric for
    conversion/retention. Awareness is judged on CPM/reach, where a low ROAS is expected.
  - Anchored bands make scores comparable across campaigns and runs.
  - Explicit rules cover the edge cases: untracked campaigns score ≤30, and zero spend means ROAS is undefined.
  - Temperature is 0.
- **Failure handling:**
  1. Malformed or out-of-range output is retried (default 2 retries), and **the validation error is sent back
     to the model** so it can correct itself rather than blindly re-rolling.
  2. API/network errors are retried. The Groq SDK also backs off on 429s.
  3. After the retries run out, there's a **deterministic fallback**: the channel comes from a rule map, and
     objective/score are left NULL rather than invented. The row is stored with `enrichment_status=fallback`,
     and the errors appear in the summary.
- **Self-check / eval:** every LLM result is compared with deterministic rules.
  - The channel must match the rule map whenever the rule is confident.
  - An untracked campaign must not score above 30.
  - A conversion/retention campaign with ROAS < 1.5 must not score above 60.
  - Disagreements are stored in `eval_flags` and returned in the ingest summary. They're flagged, not
    overwritten, because they're for review.
- **Insights:** SQL aggregates by channel, and the LLM only interprets them, with a schema of 2–3 observations
  that cite campaign ids. If the LLM fails, the aggregates are still returned along with `llm_error`.
- **Versioning:** `prompt_version` and `llm_model` are stored on every row, so scores from different prompts
  are never compared by accident.

## What I'd do with more time

- **Alembic migrations** instead of `create_all`.
- **Async/concurrent enrichment** with a semaphore sized to the rate limit. It's sequential now, which is fine
  for 14 rows.
- Run ingest as a **background job** (return `202` + run id), since a large file would block the request.
- A **golden-set eval:** hand-label the channel, objective and score band for these 14 rows, then track
  accuracy for each prompt version or model change.
- Score with **portfolio context** (benchmark against the brand's own median ROAS per channel) instead of fixed
  bands.
- Send duplicate-id and negative-spend rows to a **quarantine table** for ad-ops to fix, with re-submission.
- Auth, request ids and structured logging/metrics: LLM latency, retry rate and fallback rate.
