# YPK Job Scraper — Project Context

## Instructions for the Agent

> **If you are an agent reading this file**, understand that this project is **still in the developing phase**. Read through the sections below to understand the purpose, architecture, and current state of the pipeline. Not everything is finalized — some decisions are pending. Use this document as the source of truth for what the project aims to do and how it is structured.
>
> **Important:** Since this project is actively under development, you should **update this context file** whenever you make changes that affect the architecture, pipeline steps, configuration, or any pending decisions. Keep this document in sync with the actual state of the project so that future agents (and humans) have accurate context.
>
> **Development style — strictly incremental:** This project is being built **one step at a time**. When the user asks for something, do exactly that step and nothing more — do not pre-implement future pipeline stages, do not add features that weren't requested, and do not scaffold ahead. You may **point out** if the user seems to be heading in a wrong direction, but **do not move ahead of them**. Always stay one step in the right direction, not several.
>
> **Two non-negotiable rules (full text in "Important Rules of Engagement" below):**
> 1. After ANY edit to `profiles.yaml`, `skill_aliases.yml`, or `location_aliases.yml`, you MUST run `python verification/validate_profiles.py` and it MUST exit 0.
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
│  Step 1: Read Config + Matching State                 [IMPLEMENTED] │
│    • Load companies list (slug, company_name, ats_platform, enable) │
│    • Load ATS platforms (delay_seconds, max_concurrency)            │
│    • Load profiles (profiles.yaml) — filtered to enable: true       │
│    • Load skill/role aliases (skill_aliases.yml)                    │
│    • Load location aliases (location_aliases.yml)                   │
│    • Per Rule 1: validate_profiles(raise_on_error=True) runs at     │
│      startup; pipeline refuses to start if validation fails.        │
│      State lives in main.MatchingState, built by load_matching_state│
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
│    • Only NEW jobs are passed downstream to Step 4                  │
│                                                                     │
│  Step 4: Match per Profile                            [IMPLEMENTED] │
│    • For each NEW job, extract JD text via _extract_jd_text         │
│      (concatenates title + department + team + description +        │
│      content + body + location — whichever the connector exposes)   │
│    • For each enabled profile, evaluate every matching_rule against │
│      the JD via verification.match_profile.match_profile_against_jd │
│    • A profile matches iff EVERY rule passes (AND across rules)     │
│    • Both boolean and scored rule types are evaluated in a single   │
│      per-profile pass (the original "pre-filter then score" split   │
│      collapsed into one rule walk; rule order in profiles.yaml is   │
│      now the only knob)                                             │
│    • Per-profile match errors are caught + logged; one broken       │
│      profile never aborts the rest of the loop                      │
│                                                                     │
│  Step 5: Notify on Match                              [IMPLEMENTED] │
│    • For each (profile, job) where the profile matched, main.notify │
│      is called with (profile, job, match_result)                    │
│    • Always prints a [MATCH] visibility line for local observability│
│    • Sends a Telegram message to profile.chat_id using the bot      │
│      token at local_secrets.telegram_bot_token; message body has    │
│      the job title, company/location (when present), the matched   │
│      profile name, and a clickable link to the job URL              │
│    • Graceful degradation — each of the following just logs ONE     │
│      `notify:` breadcrumb and returns (no exception, no retry):     │
│        - profile has no chat_id (owner hasn't DM'd the bot yet)     │
│        - local_secrets.py missing                                   │
│        - telegram_bot_token empty / whitespace                      │
│        - Telegram sendMessage HTTP / transport / API error          │
│    • Untrusted JD content (title, company, location, url) is HTML-  │
│      escaped before going into the Telegram message body. Telegram  │
│      parse_mode=HTML; link previews left enabled so the recipient   │
│      gets the job's preview card                                    │
│    • Real notification target (slack / email / webhook / queue) is  │
│      a pending decision (see "Decisions Still Pending")             │
│    • Notify exceptions are caught + logged; a buggy future notify   │
│      never aborts the tick or skips subsequent profiles             │
│                                                                     │
│  Background: Telegram chat_id sync                    [IMPLEMENTED] │
│    • At startup + every TELEGRAM_SYNC_INTERVAL_SECONDS (10 min),    │
│      main.py runs _run_telegram_sync_safely() at the TOP of a tick  │
│    • Calls verification.sync_telegram_chat_ids.sync_chat_ids        │
│    • Scans recent bot DMs for `profile=<profile_name>`              │
│    • Writes the sender's chat_id into the matching profile entry    │
│      (line-surgical edit — preserves every comment in profiles.yaml)│
│    • If any change applied, sets _config_changed = True so the same │
│      tick re-runs Rule-1 validation and rebuilds matching_state     │
│      with the new chat_id values visible to the matcher             │
│    • Never raises — Telegram outages / missing token / empty token  │
│      are logged and the pipeline continues. Telegram sync is a      │
│      convenience, not a hard dependency of the matcher.             │
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
| `chat_id`              | Telegram chat_id (int) that owns this profile, or empty if not yet bound. Auto-populated by `verification/sync_telegram_chat_ids.py` when the owner DMs the bot a message containing `profile=<profile_name>`. Do NOT edit by hand. |
| `enable`               | Boolean; only `true` profiles are evaluated. **Default to `false`** for every new profile — flip to `true` only after the profile is tuned and you actually want it producing matches. |
| `years_of_experience`  | Float, expressed as a **true decimal year** — i.e. 2 years 8 months = `2 + 8/12 = 2.67`, NOT `2.8`. |
| `locations`            | List of preferred locations, each a dotted ref `locations.<region>.<canonical>` into `location_aliases.yml`. |
| `matching_rules`       | List of rules. **ALL rules must pass** for a job to match the profile (AND across the list).         |

#### Matching-rule DSL

Two rule types (the full grammar with examples lives at the top of `profiles.yaml` — that file is the spec, this section is the summary):

1. **`type: boolean`** — boolean expression over group references with `and` / `or` / `not` / `(...)`. A group reference is a dotted path like `skills.languages.go`, `roles.role_families.backend_engineer`, or `locations.work_modes.remote`. A group is true if any of its aliases match the JD.
2. **`type: scored`** — comma-separated `<group_ref>=<weight>` pairs followed by one or more thresholds (currently `totalscore>=N`; `distinct_matches>=N` reserved for future). The matcher sums weights of every matched group and checks all thresholds.

The matcher lives in **`verification/match_profile.py`** and is wired into `main.py` so every NEW job is evaluated against every enabled profile during the tick loop (see Pipeline Steps 4 & 5 above). The module is also runnable standalone as a CLI for candidate-driven profile validation against a sample JD — see **Scripts** below.

### Skill aliases — `skill_aliases.yml`

Canonical skills and roles + their aliases, grouped by category. Two top-level keys:

| Key       | Purpose                                                                                                  |
|-----------|----------------------------------------------------------------------------------------------------------|
| `skills`  | Categorized canonical skills (languages, databases, cloud, frameworks, ML, etc.) with their alias lists. |
| `roles`   | Categorized canonical role titles (role families, seniority modifiers, domain qualifiers, etc.).         |

Match rules (enforced by `verification/match_profile.py`):

- Case-insensitive.
- All whitespace runs are collapsed to a single space.
- Punctuation is **NOT** normalized — kept verbatim because aliases like `c++`, `c#`, `pl/sql`, `next.js`, `back-end` depend on punctuation being preserved. Instead, the alias files enumerate every separator variant explicitly (`back-end` / `back end` / `backend`). This is a deliberate deviation from the "intended" rule originally documented at the top of `skill_aliases.yml`; the alias-file header comment is now stale on this point.
- **Word-boundary matching is mandatory** (`\bml\b` does not match `html`). Substring matching is forbidden. The matcher uses a custom boundary character class `[A-Za-z0-9_+#]` (extends `\w` with `+` and `#`) so `c++` matches as a whole token and is not confused with `cc++` or `c+++`.
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

- **`main.py`** — the continuous pipeline. Ticks every 5 seconds. Now also: validates the alias config at startup, builds a `MatchingState`, and for every NEW job calls `_match_and_notify(job, matching_state)` which evaluates each enabled profile and invokes `notify(profile, job, match_result)` on a match. The `notify` function is a deliberate stub (prints `[MATCH] ...`) — its real implementation is one of the pending decisions below.
- **`verification/`** — Python package containing standalone scripts for verifying/testing parts of the system in isolation. **Two kinds of files live here now: (a) runtime-importable modules** like `match_profile.py` and `validate_profiles.py` that `main.py` actually imports from, and **(b) standalone scripts** like `try_fetch.py` / `audit_slugs.py` that are NOT part of the runtime pipeline. New scripts of either kind MUST live here. Each one resolves project-root paths via `Path(__file__).resolve().parent.parent` and adds the project root to `sys.path` before any cross-imports, so each module works whether invoked as `python verification/<name>.py` or as `python -m verification.<name>` and regardless of CWD. Currently provided:
  - **`verification/validate_profiles.py`** — validates `profiles.yaml` against `skill_aliases.yml` and `location_aliases.yml`. CLI: `python verification/validate_profiles.py` (exit 0 = OK, exit 1 = errors printed). Library: `from verification.validate_profiles import validate_profiles, ValidationError`. `main.py` calls this at startup via `validate_profiles(raise_on_error=True)`; the pipeline refuses to start on failure.
  - **`verification/match_profile.py`** — THE matcher. Public API: `match_profile_against_jd(profile, jd_text, skills_root, roles_root, locations_root) -> MatchResult`, plus loaders `load_alias_files()` / `load_profile(name)` / `load_all_profiles()` and the dataclasses `MatchResult` / `RuleResult` / `GroupMatch` (the latter carries per-rule debug info — which aliases matched and where in the JD). Imported by `main.py`. Also runnable as a CLI for the validation-by-candidate use case: `python verification/match_profile.py <profile_name> --jd-file path/to/jd.txt -v` (exit 0 matched, 1 not matched, 2 usage/IO error; `--all` evaluates every profile against the JD).
  - **`verification/try_fetch.py`** — standalone CLI tester for a single `(ats, slug)` pair. No DB, no loop, no rate limiting. Use to verify a slug, inspect what jobhive returns, or debug a connector. See README for usage.
  - **`verification/audit_slugs.py`** — bulk-audits every `(ats, slug)` in `config.yml` in parallel and prints a per-entry + grouped summary. Use after editing the company list.
  - **`verification/sync_telegram_chat_ids.py`** — calls the Telegram Bot API's `getUpdates`, scans every recent message for `profile=<profile_name>`, and writes the sender's `chat_id` into the matching profile entry in `profiles.yaml`. Run this once after a profile owner DMs the bot. Requires `telegram_bot_token` in `local_secrets.py`. Supports `--dry-run` to preview without writing.
  - **`verification/test_match_profile.py`** — pytest suite for the matcher (~236 tests). Covers every regex boundary edge case (`c++`, `c#`, `ml`/`html`, `js`/`jsx`, multi-word with whitespace runs), boolean DSL parser (precedence, parens, errors), scored DSL (thresholds, `distinct_matches`), loader behavior, and end-to-end matches against the live alias files.
  - **`verification/test_main_match_integration.py`** — pytest suite for the runtime wiring in `main.py` (~42 tests): `_extract_jd_text`, `MatchingState`, `load_matching_state`, `notify` stub, `_match_and_notify` (including per-profile/per-notify exception isolation), and `process_fetch_result` integration (NEW vs repeat vs rate-limited vs failed paths). Run together with `pytest verification/test_main_match_integration.py verification/test_match_profile.py -q`.

---

## Important Rules of Engagement

> Two non-negotiable rules. Future agents (and humans) MUST honor both.

### Rule 1 — Validate profiles after EVERY edit

After **any** change to `profiles.yaml`, `skill_aliases.yml`, or `location_aliases.yml`, run:

```bash
python verification/validate_profiles.py
```

It must exit 0. Do NOT commit, do NOT proceed to other changes. The validator catches duplicate profile names, malformed `years_of_experience`, broken DSL syntax, and — most importantly — every dotted ref like `skills.languages.go` that doesn't resolve into the alias files. A typo here would silently degrade matching, which is exactly the failure mode this project cannot tolerate.

`main.py` already calls `validate_profiles(raise_on_error=True)` at startup (inside the config-load block, before `load_matching_state()`) and the pipeline refuses to start if validation fails. **Do not weaken or remove this check.** If you change the startup flow, the validator MUST still run before any matching state is built. Silent matching on broken alias config is the worst failure mode this project can produce.

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

- Telegram is the only delivery channel today (`main.notify` -> `verification.sync_telegram_chat_ids.send_message`). Whether to add a second channel (queue / webhook / Slack / email / file) or change the message body shape is still open. The `match_result` argument is currently unused by the body — keep it in the signature for now so a future implementation that wants to attach the per-rule scoring breakdown doesn't have to refactor call sites.
- How to extract `years_of_experience` ranges from JD prose (regex? LLM? skip for v1?). The matcher currently does NOT consult the profile's `years_of_experience` field at all.
- How `years_of_experience` on the profile interacts with whatever range a JD declares (hard filter vs. score boost).
- Output file format and location for matched jobs (separate from the notification channel above — this is the "audit log" question).
- Whether to detect rate-limit errors via typed exceptions instead of the current string-based heuristic (depends on what jobhive raises).
- Whether profiles.yaml / skill_aliases.yml / location_aliases.yml should support live reload (SIGHUP?) or always require a process restart. Today they load once at startup alongside `config.yml`.
- Whether the alias-file header comments in `skill_aliases.yml` / `location_aliases.yml` (which still say "punctuation normalized to a single space") should be updated to match the matcher's actual behavior (it does NOT normalize punctuation). Context.md is the source of truth; the file headers are currently stale on this single point.

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
- **Matcher conventions:** case-insensitive; whitespace runs collapsed to single space; punctuation **kept verbatim** (alias files enumerate separator variants explicitly); **mandatory word-boundary matching** using an extended character class `[A-Za-z0-9_+#]` so `c++` and `c#` match as whole tokens. Ambiguous bare aliases like `go`, `r`, `c` are deliberately omitted from the alias files.
- **Matcher lives at `verification/match_profile.py`.** Public API: `match_profile_against_jd(profile, jd_text, skills_root, roles_root, locations_root) -> MatchResult`. The module is importable by runtime code AND runnable as a standalone CLI for candidate-driven validation (a user adding a profile can paste a known-good JD and see per-rule pass/fail with which aliases hit where).
- **A profile passes iff EVERY rule in `matching_rules` passes** (AND across the rules list). `boolean` rules pass iff their expression evaluates to `True`; `scored` rules pass iff every threshold clause is satisfied.
- **A group reference is TRUE iff at least one of its aliases is found in the JD** (the JD search uses the conventions above). The matcher does NOT short-circuit alias scanning per group — every alias is tested so the per-group debug output can show every hit.
- **Boolean DSL is evaluated via a hand-written recursive-descent parser**, not `eval()` / `exec()` — per the project's secure-Python rule, no YAML-sourced expression ever hits Python's evaluator.
- **Per-job evaluation is per-profile, notify per match.** For each NEW job, every enabled profile is evaluated independently; `main.notify(profile, job, match_result)` is called once for each profile that matches. (The earlier "at least one profile vs. emit per profile" pending decision is resolved in favor of the latter.)
- **`main.notify(profile, job, match_result)` delivers via Telegram.** It always prints the `[MATCH]` visibility line, then sends a Telegram message to `profile["chat_id"]` using the bot token from `local_secrets.telegram_bot_token`. The message body contains the job title, company / location (when present), the matched profile name, and a clickable link to the job URL. Missing chat_id, missing `local_secrets.py`, empty token, and any Telegram API / transport error each log ONE `notify:` breadcrumb and return — the pipeline never crashes on delivery failure. `match_result` is currently unused by the body but kept in the signature for future use.
- **JD text fed to the matcher** is built by `main._extract_jd_text(job)` which concatenates these jobhive job-model attributes in order (skipping missing/empty): `title, department, team, description, content, body, location`. Per-field weighting is NOT applied at this layer — the matcher's scored rules and the alias files do all the weighting.
- **Startup validation:** `main.py` calls `validate_profiles(raise_on_error=True)` inside the config-load block, BEFORE `load_matching_state()`, and refuses to start the pipeline if validation fails. See Rule 1.
- **One-time load:** profiles, skill aliases, and location aliases are loaded once at startup (alongside `config.yml`). Restart the process to pick up edits. (Live reload is on the pending list.)
- **Defensive failure isolation in `_match_and_notify`:** per-profile match exceptions and per-call notify exceptions are caught and logged; one broken profile or one buggy notify never aborts the tick or skips subsequent profiles.

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
