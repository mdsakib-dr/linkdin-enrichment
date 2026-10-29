# LinkedIn Lead Generation CLI

A Python CLI that automates LinkedIn lead generation using two [Apify](https://apify.com) actors and saves enriched leads to PostgreSQL.

**Pipeline:** `search` → `enrich` → `save`

1. **Search** — runs [`harvestapi/linkedin-profile-search`](https://apify.com/harvestapi/linkedin-profile-search) for each query and collects profile URLs (deduped) until `MAX_LEADS`.
2. **Enrich** — batches URLs (~50/batch) through [`dev_fusion/linkedin-profile-scraper`](https://apify.com/dev_fusion/linkedin-profile-scraper) (`2SyF0bVxmgGr8IVCZ`) with bounded concurrency.
3. **Save** — upserts each lead into Postgres keyed on `linkedin_url`, so re-runs update rather than duplicate.

Cost is estimated as it runs and the pipeline **hard-stops** before exceeding `MAX_BUDGET_USD`.

---

## Requirements

- Python 3.10+
- A running PostgreSQL instance
- An Apify API token

## Project layout

```
src/
  apify_client.py   # Apify REST wrapper (search + enrich), retries, sync/async fallback
  models.py         # SQLAlchemy models (Lead)
  db.py             # engine/session + ON CONFLICT upsert
  pipeline.py       # orchestration, cost/budget tracking, concurrency
  config.py         # .env loading + validation
  main.py           # CLI entry point (click)
alembic/            # migrations
requirements.txt
.env.example
```

---

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create the database

```bash
createdb linkedin_leads
# or via psql:
#   psql -c "CREATE DATABASE linkedin_leads;"
```

### 3. Configure environment

```bash
cp .env.example .env
# then edit .env
```

| Variable         | Required | Default | Description                                                        |
|------------------|----------|---------|--------------------------------------------------------------------|
| `APIFY_TOKEN`    | yes      | —       | Apify API token.                                                   |
| `DATABASE_URL`   | yes      | —       | Postgres URL, e.g. `postgresql+psycopg2://user:pass@localhost:5432/linkedin_leads` |
| `SEARCH_QUERIES` | yes*     | —       | Comma-separated queries. *Optional if you pass `--queries`.        |
| `MAX_LEADS`      | no       | `100`   | Max leads collected across all queries.                           |
| `MAX_BUDGET_USD` | no       | `5.00`  | Hard cap on estimated spend.                                       |

### 4. Run migrations

```bash
alembic upgrade head
```

> Quick alternative for local testing: `python -m src.main --init-db ...` creates
> the table directly (skips Alembic). Use migrations for anything real.

---

## Usage

```bash
python -m src.main --queries "Senior HR Manager Dhaka" --max-leads 100
```

More options:

```bash
# Multiple queries (repeat the flag, or pass a comma-separated string)
python -m src.main \
  --queries "Senior HR Manager Dhaka" \
  --queries "Senior HR Manager Chattogram" \
  --max-leads 200 --max-budget 10.00

# Use queries from .env, create the table on the fly, verbose logs
python -m src.main --init-db -v
```

CLI flags override `.env` values. Run `python -m src.main --help` for the full list.

### Sample output

```
14:02:11 INFO    main: Starting pipeline: 1 queries, max_leads=100, budget=$5.00
14:02:11 INFO    src.apify_client: Searching: 'Senior HR Manager Dhaka' (max 100)
14:02:19 INFO    src.pipeline: Search 'Senior HR Manager Dhaka': 100 items (~4 page(s)); running cost $0.40
14:02:19 INFO    src.pipeline: Collected 100 unique profile URLs from search.
14:02:19 INFO    src.pipeline: Enriching 100 profiles in 2 batch(es), concurrency 3.
14:02:19 INFO    src.pipeline: Enriching batch 1/2 (50 profiles)...
14:02:52 INFO    src.pipeline: Batch 1/2: saved 50 lead(s).
...
================================================
  RUN SUMMARY
================================================
  Profiles found (search):   100
  Profiles enriched:         98
  Enrichment failures:       2
  Leads saved (upserted):    98
  Leads with email:          61
  Leads with phone:          22
  Estimated total cost:      $1.38
================================================
```

---

## How it works — notable details

- **Upsert:** `INSERT ... ON CONFLICT (linkedin_url) DO UPDATE` via
  SQLAlchemy's `postgresql.insert`. `created_at` is preserved; the mutable
  columns and `updated_at` refresh on re-run. Rows are also deduped within a
  batch before insert (Postgres rejects duplicate conflict keys in one statement).
- **Budget guard:** search charges `$0.10`/page and enrichment `$0.01`/profile
  (`$10`/1000). Before each search query and each enrich batch the pipeline
  checks whether the next action would exceed `MAX_BUDGET_USD` and stops early
  if so (the summary notes it).
- **Resilience:** all Apify calls retry up to 3× with exponential backoff
  (`tenacity`); only transient statuses (429/5xx) and network errors retry.
  The sync endpoint falls back to async-run-and-poll on timeout.
- **Concurrency:** enrichment batches run through a `ThreadPoolExecutor` capped
  at 3 workers. Per-profile enrichment failures are logged and skipped without
  crashing the batch.

## Troubleshooting

- **`Configuration error(s)`** on startup — a required env var is missing;
  the message lists exactly which. Fix `.env` and retry.
- **`psycopg2` install fails** — ensure Postgres client headers are available,
  or rely on the bundled `psycopg2-binary` wheel (already in `requirements.txt`).
- **Actor input rejected** — actor input schemas can change; adjust the
  `run_input` dicts in `src/apify_client.py` (`search_profiles` / `enrich_profiles`).

## Notes

- The two Apify actors are third-party; respect LinkedIn's Terms of Service and
  applicable data-protection law (GDPR, etc.) when scraping and storing personal data.
