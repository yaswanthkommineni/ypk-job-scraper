# YPK Job Scraper — Project Context

## Instructions for the Agent

> **If you are an agent reading this file**, understand that this project is **still in the developing phase**. Read through the sections below to understand the purpose, architecture, and current state of the pipeline. Not everything is finalized — some decisions are pending. Use this document as the source of truth for what the project aims to do and how it is structured.
>
> **Important:** Since this project is actively under development, you should **update this context file** whenever you make changes that affect the architecture, pipeline steps, configuration, or any pending decisions. Keep this document in sync with the actual state of the project so that future agents (and humans) have accurate context.
>
> **Development style — strictly incremental:** This project is being built **one step at a time**. When the user asks for something, do exactly that step and nothing more — do not pre-implement future pipeline stages, do not add features that weren't requested, and do not scaffold ahead. You may **point out** if the user seems to be heading in a wrong direction, but **do not move ahead of them**. Always stay one step in the right direction, not several.

---

## Purpose

The goal of this project is to **beat the competition in the job market** by fetching job postings directly from ATS (Applicant Tracking System) APIs in near real-time, rather than relying on delayed aggregators like LinkedIn or other job boards.

A continuously-running script (pipeline) hits ATS APIs directly for a configured list of companies, filters the results against one or more candidate profiles, and outputs high-quality matches.

---

## Tool

This project uses **[jobhive-py](https://github.com/nicholasgasior/jobhive)** with the scrapers extra:

```
jobhive-py[scrapers]
```

---

## Pipeline Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        YPK Job Scraper Pipeline                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Step 1: Read Config                                  [IMPLEMENTED] │
│    • Load companies list (slug, company_name, ats_platform, enable) │
│    • Load ATS platforms (delay_seconds, max_concurrency)            │
│    • Profiles: NOT YET in config — section unused                   │
│                                                                     │
│  Step 2: Rate-Limited Fetch                           [IMPLEMENTED] │
│    • For each platform: skip if in cooldown (rate-limit backoff)    │
│    • Skip if last platform fetch was within delay_seconds           │
│    • Select up to max_concurrency companies (oldest fetch first)    │
│    • Fetch them in parallel via ThreadPoolExecutor                  │
│                                                                     │
│  Step 3: Receive Jobs                                 [IMPLEMENTED] │
│    • Each fetched job is recorded in recent_job_ids                 │
│      (24h dedupe, key = "{slug}#{id}")                              │
│    • Printed with [NEW] / [REPEAT] marker plus posted-time          │
│    • Jobs themselves are NOT persisted — they will be handed off    │
│      to a downstream consumer in a future step                      │
│                                                                     │
│  Step 4: Pre-Filter (Filter-1)                            [PENDING] │
│    • Apply basic filtering criteria per profile                     │
│    • A job passes if it matches at least one profile's filter-1     │
│                                                                     │
│  Step 5: Advanced Filter + Scoring                        [PENDING] │
│    • For each remaining job, score how well it matches each profile │
│    • If score is above threshold → emit to output                   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Configuration

The config file is **`config.yml`** (YAML, decided). It contains:

### `ats_platforms`

Map of platform name → settings:

| Field             | Description                                                    |
|-------------------|----------------------------------------------------------------|
| `delay_seconds`   | Minimum interval between fetches for this platform             |
| `max_concurrency` | Max companies fetched in parallel per tick for this platform   |
| `notes`           | Free-form description / rationale                              |

### `companies`

A list of companies, each with:

| Field          | Description                                                     |
|----------------|-----------------------------------------------------------------|
| `slug`         | Per-ATS identifier (passed to jobhive's `get_scraper`)          |
| `company_name` | Human-readable company name                                     |
| `ats_platform` | Which ATS platform to hit (must exist under `ats_platforms`)    |
| `enable`       | Boolean; only `true` companies are fetched                      |

### Profiles

The system is planned to support **multiple candidate profiles** for filtering and scoring (Steps 4 & 5). **Not yet present** in the config or code.

---

## Local Database (SQLite)

A local SQLite database (`pipeline_state.db`) stores operational metadata. Tables currently in use:

| Table                       | Purpose                                                                       |
|-----------------------------|-------------------------------------------------------------------------------|
| `company_last_fetch`        | Last successful fetch timestamp per company slug                              |
| `ats_platform_last_fetch`   | Last successful fetch timestamp per ATS platform                              |
| `ats_platform_rate_limit`   | Active rate-limit cooldowns per platform (cooldown_period, last_rate_limited_at). 1h initial, doubles per hit, deleted on success |
| `recent_job_ids`            | Dedupe table for fetched job keys (`{slug}#{id}`). Auto-cleaned hourly; rows older than 24h are dropped |

Index `idx_recent_job_ids_fetched_at` exists on `recent_job_ids.fetched_at` to keep the hourly cleanup cheap.

---

## Scripts

- **`main.py`** — the continuous pipeline. Ticks every 5 seconds.
- **`try_fetch.py`** — standalone CLI tester for a single `(ats, slug)` pair. No DB, no loop, no rate limiting. Use to verify a slug, inspect what jobhive returns, or debug a connector. See README for usage.

---

## Decisions Still Pending

- Exact filter-1 criteria per profile
- Advanced filtering / scoring algorithm details
- Downstream consumer interface for fetched jobs (queue? webhook? file?)
- Output file format and location for matched jobs
- Profile schema (and where it lives — same `config.yml` or separate file)
- Whether to detect rate-limit errors via typed exceptions instead of the current string-based heuristic (depends on what jobhive raises)

---

## Resolved Decisions (for the record)

- **Config format:** YAML (`config.yml`).
- **Initial rate-limit cooldown:** 1 hour, doubles per consecutive rate-limit hit, cleared on success.
- **Per-platform delay values:** set in `config.yml` under `ats_platforms.*.delay_seconds`.
- **Per-platform concurrency:** set in `config.yml` under `ats_platforms.*.max_concurrency`.
- **Job dedupe window:** 24 hours, keyed as `{company_slug}#{job_id}`.
- **Fetcher library:** [jobhive-py](https://pypi.org/project/jobhive-py/) with `[scrapers]` extra.

---

## Running

```bash
pip install -r requirements.txt
python main.py
```

For debugging a single slug without the full pipeline:

```bash
python try_fetch.py <ats> <slug>
python try_fetch.py --examples
```
