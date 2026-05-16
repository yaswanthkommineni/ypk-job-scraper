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
│  Step 1: Read Config                                                │
│    • Load companies list (name, slug, ATS platform)                 │
│    • Load profiles                                                  │
│                                                                     │
│  Step 2: Rate-Limited Fetch                                         │
│    • For each company, check last hit time (from SQLite DB)         │
│    • Respect per-ATS rate limits                                    │
│    • If eligible → fetch latest jobs from the ATS API               │
│                                                                     │
│  ── From here, multiple threads per company ──                      │
│                                                                     │
│  Step 3: Receive Jobs                                               │
│    • Input: list of jobs extracted in Step 2 for a specific company │
│                                                                     │
│  Step 4: Pre-Filter (Filter-1)                                      │
│    • Apply basic filtering criteria per profile (TBD)               │
│    • A job passes if it matches at least one profile's filter-1     │
│                                                                     │
│  Step 5: Advanced Filter + Scoring                                  │
│    • For each remaining job, score how well it matches each profile │
│    • If score is above threshold → store in output file             │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Configuration

The config file contains:

### Companies

A list of companies, each with:

| Field        | Description                                              |
|--------------|----------------------------------------------------------|
| `name`       | Human-readable company name                              |
| `slug`       | Unique identifier / URL slug for the company             |
| `ats_platform` | Which ATS platform to hit (limited set of supported types) |

### Profiles

The system supports **multiple candidate profiles** stored in the config file. Each profile defines the filtering and matching criteria used in Steps 4 and 5.

---

## Local Database (SQLite)

A local SQLite database stores operational metadata:

- **Last hit time per company** — used together with per-ATS rate limits to decide whether a company is eligible for fetching in the current run.
- Additional fields TBD as the project evolves.

---

## Decisions Still Pending

- Exact filter-1 criteria per profile
- Advanced filtering / scoring algorithm details
- Output file format and location
- Specific rate limit values per ATS platform
- Config file format (YAML, JSON, TOML — TBD)

---

## Running

```bash
# (TBD) Example:
source venv/bin/activate
python main.py
```
