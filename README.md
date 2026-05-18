# ypk-job-scraper

A continuously-running pipeline that fetches job postings directly from ATS (Applicant Tracking System) APIs, bypassing delayed aggregators like LinkedIn. The goal is to see new jobs as soon as they appear, giving you a competitive edge in the job market.

See [context.md](context.md) for full architectural details and current development status.

## Files at a glance

| File | What it is | Status |
|---|---|---|
| `main.py` | The continuous pipeline (tick every 5s). | implemented |
| `try_fetch.py` | One-shot CLI tester for a single `(ats, slug)` pair. | implemented |
| `config.yml` | ATS-platform rate-limit settings + the list of companies to fetch. | implemented |
| `skill_aliases.yml` | Canonical skills & roles + every alias the matcher should treat as equivalent. | config only — matcher pending |
| `location_aliases.yml` | Canonical locations (India + US California + work modes) + aliases. | config only — matcher pending |
| `profiles.yaml` | Candidate profiles: years of experience, preferred locations, and boolean / scored matching rules. | config only — matcher pending |
| `validate_profiles.py` | Validator for the three files above. **Run it after every edit.** | implemented |
| `pipeline_state.db` | SQLite operational state (last-fetch timestamps, rate-limit cooldowns, 24h job-id dedupe). Auto-created. | implemented |

> **Heads-up:** the alias files and `profiles.yaml` are the *spec* for the matcher (Steps 4 & 5 of the pipeline). The matcher itself is not yet built, so editing those files doesn't yet change runtime behavior — but the validator already checks them end-to-end so they stay consistent.

## Two non-negotiable rules

1. **After ANY edit to `profiles.yaml`, `skill_aliases.yml`, or `location_aliases.yml`, run `python validate_profiles.py`.** It must exit 0. The validator catches duplicate profile names, malformed `years_of_experience`, broken matching-rule syntax, and — most importantly — every dotted reference like `skills.languages.go` that doesn't resolve into the alias files.
2. **Every skill on a candidate's resume MUST have an alias group in `skill_aliases.yml` before being referenced from `profiles.yaml`.** If a skill is missing from the alias file, the matcher will silently fail to match jobs requiring that skill, with no warning. Workflow: list skills → grep `skill_aliases.yml` → add anything missing → validate.

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

### 2. Configure which companies to fetch — `config.yml`

- Under `ats_platforms`, tune `delay_seconds` and `max_concurrency` per platform if needed.
- Under `companies`, flip `enable: false` to `enable: true` for any company you want to fetch.

### 3. (Optional, for the future matcher) Configure your candidate profiles

Three files cooperate to drive matching once the matcher is built. You can fill them in now and they'll be picked up automatically when the matcher lands.

- **`skill_aliases.yml`** — canonical skills and roles + their aliases. Two top-level keys: `skills:` (languages, databases, cloud, frameworks, ML, etc.) and `roles:` (role families, seniority modifiers, domain qualifiers). The matcher will match aliases case-insensitively with **word boundaries** (`\bml\b` does not match `html`). Ambiguous bare aliases like `go`, `r`, `c` are deliberately omitted — see the inline `# NOTE:` comments.
- **`location_aliases.yml`** — same shape, scoped to India + US California + orthogonal work modes (`remote`, `hybrid`, `onsite`, `relocation`, `visa_sponsorship`).
- **`profiles.yaml`** — one entry per candidate. Each profile has `profile_name`, `enable` (**defaults to `false` for new profiles**), `years_of_experience` (true-decimal years — `2y 8m = 2.67`, NOT `2.8`), `locations` (list of dotted refs like `locations.india.bengaluru`), and `matching_rules` (a list of `boolean` and/or `scored` rules over dotted refs like `skills.languages.go`). All rules in the list must pass for a profile to match a job. Header of the file has a cheat-sheet with examples; full grammar lives in [context.md](context.md).

### 4. Validate after every config edit

```bash
python validate_profiles.py
```

Exit code `0` = OK, exit code `1` = one or more errors printed. The validator checks all three YAML files together: profile shape, unique names, `years_of_experience` bounds, location refs resolve, matching-rule DSL parses, and every group reference like `skills.languages.go` resolves to a real entry in the alias files.

You can also import it from Python:

```python
from validate_profiles import validate_profiles, ValidationError
errors = validate_profiles()                   # returns list[str], [] = OK
validate_profiles(raise_on_error=True)         # or raise on any issue
```

### 5. Run the fetcher

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
