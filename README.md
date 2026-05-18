# ypk-job-scraper

A continuously-running pipeline that fetches job postings directly from ATS (Applicant Tracking System) APIs, bypassing delayed aggregators like LinkedIn. The goal is to see new jobs as soon as they appear, giving you a competitive edge in the job market.

See [context.md](context.md) for full architectural details and current development status.

## Job Fetcher Features

The fetcher (`main.py`) currently does the following on each tick (every 5 seconds):

- **Config-driven** — companies, ATS platforms, per-platform `delay_seconds`, and per-platform `max_concurrency` all live in `config.yml`. Only companies with `enable: true` are fetched.
- **Per-platform delay gate** — each ATS platform is fetched at most once every `delay_seconds` (e.g., greenhouse: 5s, workday: 60s).
- **Rate-limit cooldowns with exponential backoff** — when a fetch hits a 429 / rate-limit error, the platform enters a cooldown starting at **1 hour** and **doubling** on every subsequent rate-limit hit. Cleared on the next successful fetch.
- **Oldest-fetched-first scheduling** — within a platform, companies that have never been fetched (or were fetched longest ago) are picked first, up to `max_concurrency` per tick.
- **Parallel fetches per platform** — a `ThreadPoolExecutor(max_workers=max_concurrency)` per platform runs the HTTP calls in parallel. All SQLite writes stay on the main thread (single connection, no thread-safety hazards).
- **24-hour dedupe of job IDs** — every fetched job is keyed as `{slug}#{id}` and recorded in a `recent_job_ids` table. Each job is printed with a `[NEW]` or `[REPEAT]` marker. The table is auto-cleaned hourly: rows older than 24 hours are dropped.
- **Posted-time output** — each printed job line includes the job's posted/published timestamp (best-effort across connector field names: `posted_at`, `published_at`, `date_posted`, `created_at`, etc.).
- **Persistent state** — last-fetch timestamps (per company and per platform), rate-limit cooldowns, and the dedupe table all live in `pipeline_state.db` (SQLite). State survives restarts.
- **Graceful shutdown** — `Ctrl+C` (SIGINT) and SIGTERM are caught; the current tick finishes before exit.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

> **macOS:** prefer a venv:
>
> ```bash
> python3 -m venv venv
> source venv/bin/activate
> pip install -r requirements.txt
> ```

### 2. Configure

Edit `config.yml`:
- Under `ats_platforms`, tune `delay_seconds` and `max_concurrency` per platform if needed.
- Under `companies`, flip `enable: false` to `enable: true` for any company you want to fetch.

### 3. Run

```bash
python main.py
```

Press `Ctrl+C` to stop cleanly.

## Debugging a Single Slug — `try_fetch.py`

`try_fetch.py` is a standalone CLI tester for verifying a single `(ats, slug)` pair without spinning up the full pipeline (no DB writes, no loop, no rate limiting). Use it when you suspect a wrong slug, want to inspect what jobhive returns, or want to sanity-check a connector.

```bash
# Fetch one and print parsed fields (prints all jobs by default)
python try_fetch.py greenhouse swiggy

# Optionally cap how many jobs are printed
python try_fetch.py lever atlassian --limit 5

# Also dump each job's full model_dump JSON (useful for finding field names)
python try_fetch.py ashby openai --raw --limit 1

# Smoke-test a small built-in set of known-good slugs
python try_fetch.py --examples
```

Exits with code `0` on `status: success`, non-zero otherwise — handy for chaining in shell scripts.
