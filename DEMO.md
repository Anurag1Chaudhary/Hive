# Demo commands

Run everything from the project folder:

```bash
cd ~/Hive
```

---

## 0. Setup (before the interview, once)

```bash
# Start Postgres (Docker)
docker compose up -d --wait

# Check the .env file has your Groq key
cat .env

# (Only if tests complain hive_test is missing) create the test database
docker compose exec db psql -U hive -c "CREATE DATABASE hive_test"
```

---

## 1. Start the server — TERMINAL 1 (leave it running)

```bash
cd ~/Hive
fuser -k 8000/tcp                          # stop any old server on port 8000
.venv/bin/uvicorn app.main:app --reload
```

Swagger UI (clickable alternative to curl): http://localhost:8000/docs

---

## 2. Run the demo — TERMINAL 2

### 2.1 Health check (server up, LLM key loaded)
```bash
curl -s localhost:8000/health | jq
```

### 2.2 Start from an empty database
```bash
docker compose exec db psql -U hive -c "TRUNCATE campaigns, ingest_row_results, ingest_runs RESTART IDENTITY"
```

### 2.3 Ingest — clean + LLM enrich + store (takes ~20-30s)
```bash
curl -s -X POST localhost:8000/campaigns/ingest | jq '{run_id, total, succeeded, failed, by_status}'
```
Expected: total 15, ok 12, rejected 3.

### 2.4 Per-row result: what happened to each row and why
```bash
curl -s localhost:8000/ingest/runs/1 | jq '.rows[] | {source_id, status, reasons}'
```
Point at: cmp_013 (junk), second cmp_008 (duplicate), cmp_014 (negative spend).

### 2.5 Idempotency: ingest again, no duplicates, no LLM calls
```bash
curl -s -X POST localhost:8000/campaigns/ingest | jq '.by_status'
docker compose exec db psql -U hive -c "SELECT count(*) FROM campaigns"
```
Expected: skipped_unchanged 12, rejected 3, count still 12.

### 2.6 See the LLM enrichment (channel normalisation, objective, score)
```bash
curl -s localhost:8000/campaigns | jq '.[] | {source_id, raw_channel, channel, objective, health_score, rationale}'
```
Point at: "fb", "IG", "Facebook Ads" all become `meta`; "adwords" becomes `google_ads`.

### 2.7 Filters
```bash
# by channel
curl -s 'localhost:8000/campaigns?channel=meta' | jq '.[] | {source_id, channel, health_score}'

# by minimum score
curl -s 'localhost:8000/campaigns?min_score=90' | jq '.[] | {source_id, health_score}'

# combined
curl -s 'localhost:8000/campaigns?channel=meta&min_score=85' | jq '.[] | {source_id, health_score}'

# by objective
curl -s 'localhost:8000/campaigns?objective=awareness' | jq '.[] | {source_id, objective, health_score}'

# low performers (where to look first)
curl -s 'localhost:8000/campaigns?max_score=50' | jq '.[] | {source_id, health_score, rationale}'
```

### 2.8 Get one campaign
```bash
# a good one
curl -s localhost:8000/campaigns/cmp_004 | jq

# the untracked influencer: low score + no_performance_data flag
curl -s localhost:8000/campaigns/cmp_012 | jq '{source_id, spend, revenue, roas, health_score, rationale, data_flags}'

# the string spend "42,500" stored as a number
curl -s localhost:8000/campaigns/cmp_002 | jq '{source_id, spend, data_flags}'
```

### 2.9 Error handling
```bash
# invalid channel -> 422
curl -s -w '\nHTTP %{http_code}\n' 'localhost:8000/campaigns?channel=tiktok'

# score out of range -> 422
curl -s -w '\nHTTP %{http_code}\n' 'localhost:8000/campaigns?min_score=500'

# unknown id -> 404
curl -s -w '\nHTTP %{http_code}\n' localhost:8000/campaigns/cmp_999
```

### 2.10 Bad rows don't sink the batch (inline ingest)
```bash
curl -s -X POST localhost:8000/campaigns/ingest \
  -H 'content-type: application/json' \
  -d '{"campaigns": [
        null,
        "garbage",
        {"id":"x1","name":"Test promo","channel":"insta","spend":"1,000",
         "impressions":5000,"clicks":100,"conversions":5,"revenue":4000}
      ]}' | jq '.rows'
```
Expected: rejected, rejected, ok.

### 2.11 Portfolio insights (stretch)
```bash
curl -s localhost:8000/campaigns/insights | jq '{totals: .aggregates.totals, observations, llm_error}'
```

### 2.12 Force re-enrichment (e.g. after changing the prompt)
```bash
curl -s -X POST localhost:8000/campaigns/ingest \
  -H 'content-type: application/json' -d '{"force": true}' | jq '.by_status'
```

---

## 3. Tests (no Groq key needed — fake LLM)

```bash
.venv/bin/pytest -q
```
Expected: 28 passed.

```bash
# only one test file
.venv/bin/pytest -q tests/test_validation.py
.venv/bin/pytest -q tests/test_enrichment.py
.venv/bin/pytest -q tests/test_api.py
```

---

## 4. Look inside the database

```bash
# open a SQL shell (type \q to exit)
docker compose exec db psql -U hive

# or run single queries:
docker compose exec db psql -U hive -c "\dt"
docker compose exec db psql -U hive -c "\d campaigns"
docker compose exec db psql -U hive -c "SELECT source_id, channel, objective, health_score FROM campaigns ORDER BY health_score DESC"
docker compose exec db psql -U hive -c "SELECT id, started_at, total, succeeded, failed FROM ingest_runs"
docker compose exec db psql -U hive -c "SELECT run_id, source_id, status, reasons FROM ingest_row_results WHERE status = 'rejected'"
```

---

## 5. Git history (they will look at this)

```bash
git log --oneline
git show --stat HEAD          # what the last commit changed
```

---

## 6. Clean up before the real demo

If you inserted test row `x1` (step 2.10), reset and ingest again:

```bash
docker compose exec db psql -U hive -c "TRUNCATE campaigns, ingest_row_results, ingest_runs RESTART IDENTITY"
curl -s -X POST localhost:8000/campaigns/ingest | jq '.by_status'
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `no configuration file provided` | You're not in `~/Hive` — run `cd ~/Hive` |
| `Connection refused` on curl | Server not running — start it (step 1) |
| `address already in use` | `fuser -k 8000/tcp`, then start again |
| `503 LLM not configured` | Check `GROQ_API_KEY` in `.env`, restart the server |
| DB connection error | `docker compose up -d --wait` |
| Rows show `fallback` | LLM failed — check Terminal 1 logs for the error |
