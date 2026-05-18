# YPK Job Scraper — Project Context

## Instructions for the Agent

> **If you are an agent reading this file**, understand that this project is **still in the developing phase**. Read through the sections below to understand the purpose, architecture, and current state of the pipeline. Not everything is finalized — some decisions are pending. Use this document as the source of truth for what the project aims to do and how it is structured.
>
> **Important:** Since this project is actively under development, you should **update this context file** whenever you make changes that affect the architecture, pipeline steps, configuration, or any pending decisions. Keep this document in sync with the actual state of the project so that future agents (and humans) have accurate context.
>
> **Development style — strictly incremental:** This project is being built **one step at a time**. When the user asks for something, do exactly that step and nothing more — do not pre-implement future pipeline stages, do not add features that weren't requested, and do not scaffold ahead. You may **point out** if the user seems to be heading in a wrong direction, but **do not move ahead of them**. Always stay one step in the right direction, not several.
>
> **Two non-negotiable rules (full text in "Important Rules of Engagement" below):**
> 1. After ANY edit to `profiles.yaml`, `skill_aliases.yml`, or `location_aliases.yml`, you MUST run `python validate_profiles.py` and it MUST exit 0.
> 2. When a candidate's resume is added/updated, every skill on that resume MUST have a corresponding alias group in `skill_aliases.yml` BEFORE it is referenced from `profiles.yaml`.

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
│    • Profiles config now lives in profiles.yaml (loader: PENDING)   │
│    • Skill/role aliases: skill_aliases.yml (loader: PENDING)        │
│    • Location aliases:   location_aliases.yml (loader: PENDING)     │
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

### Profiles — `profiles.yaml`

Candidate profiles live in **`profiles.yaml`** (separate from `config.yml` so a profile change doesn't require touching the company list, and vice versa).

Each profile entry has:

| Field                  | Description                                                                                          |
|------------------------|------------------------------------------------------------------------------------------------------|
| `profile_name`         | Unique string id for this profile.                                                                   |
| `enable`               | Boolean; only `true` profiles are evaluated. **Default to `false`** for every new profile — flip to `true` only after the profile is tuned and you actually want it producing matches. |
| `years_of_experience`  | Float, expressed as a **true decimal year** — i.e. 2 years 8 months = `2 + 8/12 = 2.67`, NOT `2.8`. |
| `locations`            | List of preferred locations, each a dotted ref `locations.<region>.<canonical>` into `location_aliases.yml`. |
| `matching_rules`       | List of rules. **ALL rules must pass** for a job to match the profile (AND across the list).         |

#### Matching-rule DSL

Two rule types (the full grammar with examples lives at the top of `profiles.yaml` — that file is the spec, this section is the summary):

1. **`type: boolean`** — boolean expression over group references with `and` / `or` / `not` / `(...)`. A group reference is a dotted path like `skills.languages.go`, `roles.role_families.backend_engineer`, or `locations.work_modes.remote`. A group is true if any of its aliases match the JD.
2. **`type: scored`** — comma-separated `<group_ref>=<weight>` pairs followed by one or more thresholds (currently `totalscore>=N`; `distinct_matches>=N` reserved for future). The matcher sums weights of every matched group and checks all thresholds.

The matcher itself is **NOT YET IMPLEMENTED**. The DSL is a spec the future parser/evaluator will target.

### Skill aliases — `skill_aliases.yml`

Canonical skills and roles + their aliases, grouped by category. Two top-level keys:

| Key       | Purpose                                                                                                  |
|-----------|----------------------------------------------------------------------------------------------------------|
| `skills`  | Categorized canonical skills (languages, databases, cloud, frameworks, ML, etc.) with their alias lists. |
| `roles`   | Categorized canonical role titles (role families, seniority modifiers, domain qualifiers, etc.).         |

Match rules (intended; documented at the top of the file; the matcher will enforce them):

- Case-insensitive.
- Punctuation/hyphens/extra-whitespace normalized to a single space.
- **Word-boundary matching is mandatory** (`\bml\b` does not match `html`). Substring matching is forbidden.
- Ambiguous single-letter / English-word aliases (`go`, `r`, `c`, `cf`, `dl`, `ir`, `kg`, `tf`, etc.) are deliberately **omitted** — each removal has an inline `# NOTE:` comment.

### Location aliases — `location_aliases.yml`

Same shape as `skill_aliases.yml`. Top-level key `locations` → `<region>` → `<canonical_location>` → aliases. Scope is India + US California (per current decision) plus orthogonal `work_modes` (remote / hybrid / onsite / relocation / visa_sponsorship). Profiles reference locations via dotted paths like `locations.india.bengaluru` or `locations.work_modes.remote`.

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
- **`validate_profiles.py`** — validates `profiles.yaml` against `skill_aliases.yml` and `location_aliases.yml`. Run as `python validate_profiles.py` (exit 0 = OK, exit 1 = errors printed). Also importable: `from validate_profiles import validate_profiles, ValidationError` — the future matcher MUST call this at startup before evaluating any rules.

---

## Important Rules of Engagement

> Two non-negotiable rules. Future agents (and humans) MUST honor both.

### Rule 1 — Validate profiles after EVERY edit

After **any** change to `profiles.yaml`, `skill_aliases.yml`, or `location_aliases.yml`, run:

```bash
python validate_profiles.py
```

It must exit 0. Do NOT commit, do NOT proceed to other changes, and the matcher (when implemented) MUST refuse to start if validation fails. The validator catches duplicate profile names, malformed `years_of_experience`, broken DSL syntax, and — most importantly — every dotted ref like `skills.languages.go` that doesn't resolve into the alias files. A typo here would silently degrade matching, which is exactly the failure mode this project cannot tolerate.

When wiring the matcher into `main.py`, call `validate_profiles(raise_on_error=True)` once during startup. No exceptions.

### Rule 2 — Every resume skill MUST have aliases in `skill_aliases.yml`

When a candidate's resume is added or updated (or a new profile is created from one), every skill named on that resume MUST be reflected in `skill_aliases.yml` — either as an existing canonical/alias, or as a newly added entry. If a skill from the resume has no alias group, that profile will silently fail to match jobs requiring that skill, with no error or warning.

Workflow when editing a resume / adding a profile:

1. List every technical term on the resume (languages, frameworks, tools, cloud services, ML libraries, concepts).
2. For each term, grep `skill_aliases.yml` for it.
3. If absent, ADD it to the appropriate category (with all common aliases) BEFORE referencing it from `profiles.yaml`.
4. Run `python validate_profiles.py`.

This rule is what makes the alias file authoritative; without it, profiles drift out of sync with reality.

---

## Decisions Still Pending

- Matcher implementation itself — both the alias walker (skills/roles/locations) and the `matching_rules` DSL parser/evaluator.
- Whether a job must pass **at least one** profile's rules to be emitted, or be evaluated per-profile and emitted per profile that matches.
- How to extract `years_of_experience` ranges from JD prose (regex? LLM? skip for v1?).
- How `years_of_experience` on the profile interacts with whatever range a JD declares (hard filter vs. score boost).
- Downstream consumer interface for matched jobs (queue? webhook? file?).
- Output file format and location for matched jobs.
- Whether to detect rate-limit errors via typed exceptions instead of the current string-based heuristic (depends on what jobhive raises).

---

## Resolved Decisions (for the record)

- **Config format:** YAML (`config.yml`).
- **Initial rate-limit cooldown:** 1 hour, doubles per consecutive rate-limit hit, cleared on success.
- **Per-platform delay values:** set in `config.yml` under `ats_platforms.*.delay_seconds`.
- **Per-platform concurrency:** set in `config.yml` under `ats_platforms.*.max_concurrency`.
- **Job dedupe window:** 24 hours, keyed as `{company_slug}#{job_id}`.
- **Fetcher library:** [jobhive-py](https://pypi.org/project/jobhive-py/) with `[scrapers]` extra.
- **Profiles live in a separate file** `profiles.yaml` (not in `config.yml`).
- **Skill / role aliases live in** `skill_aliases.yml` (single file, two top-level keys: `skills`, `roles`).
- **Location aliases live in** `location_aliases.yml` (top-level `locations`, scoped to India + US California + work modes).
- **`years_of_experience` is a true decimal year** (2y 8m = 2.67, not 2.8) — convention documented at the top of `profiles.yaml`.
- **Matching-rule DSL** has two rule types — `boolean` (with `and`/`or`/`not`/parentheses) and `scored` (`group=weight`, `totalscore>=N`). Group references are dotted paths into the alias files. Full grammar documented at the top of `profiles.yaml`.
- **Matcher conventions:** case-insensitive, normalize punctuation/whitespace, **mandatory word-boundary matching** (no substring matches). Ambiguous bare aliases like `go`, `r`, `c` are deliberately omitted from the alias files.

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
