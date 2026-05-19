"""YPK Job Scraper - entry point.

Runs a continuous loop that calls `tick()` every 5 seconds.
This is the minimal skeleton; pipeline stages will be added incrementally.
See context.md for the overall architecture and development style.
"""

from __future__ import annotations

import html
import signal
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from jobhive.scrapers import get_scraper

from verification.match_profile import (
    MatchResult,
    MatcherError,
    load_alias_files,
    load_all_profiles,
    match_profile_against_jd,
)
from verification.sync_telegram_chat_ids import (
    SyncError,
    send_message,
    sync_chat_ids,
)
from verification.validate_profiles import ValidationError, validate_profiles

TICK_INTERVAL_SECONDS = 5
CONFIG_PATH = Path(__file__).parent / "config.yml"
PROFILES_PATH = Path(__file__).parent / "profiles.yaml"
DB_PATH = Path(__file__).parent / "pipeline_state.db"
INITIAL_COOLDOWN_SECONDS = 60 * 60  # 1 hour
#TODO: Change this to 24 hours once everything is set
JOB_ID_RETENTION_SECONDS = 30 * 24 * 60 * 60  # 24 hours
CLEANUP_INTERVAL_SECONDS = 60 * 60  # 1 hour
MAX_JOB_AGE_SECONDS = 30 * 24 * 60 * 60  # drop jobs posted more than 24h ago
# How often to poll Telegram for new `profile=<name>` binding messages.
# Telegram's getUpdates returns the last ~24h of messages, so the pipeline
# doesn't miss anything between polls — this just controls latency
# between a candidate DMing the bot and their chat_id landing in
# profiles.yaml. The sync ALSO runs once at startup (see
# `last_telegram_sync_at = 0.0` in `main()`), so a fresh process picks up
# any binding the owner sent before launching.
TELEGRAM_SYNC_INTERVAL_SECONDS = 10 * 60

# Job-model attributes the matcher should treat as JD content. Pulled in
# this order; missing/empty fields are skipped. Different jobhive connectors
# expose different field names, so we try the common ones and concatenate
# whatever's present. Weighting is the matcher's job, not ours.
JD_TEXT_ATTRS = (
    "title",
    "department",
    "team",
    "description",
    "content",
    "body",
    "location",
)

_config_changed = False
_stop = False


@dataclass
class MatchingState:
    """Pre-loaded data needed to match a JD against profiles at runtime.

    Built once at startup (and rebuilt alongside config reloads). Held by
    the tick loop and passed down to `process_fetch_result`, which calls
    `_match_and_notify` for every newly-seen job.

    Attributes:
      enabled_profiles: profiles from `profiles.yaml` filtered to
        `enable: true`. Empty list = matching is a no-op (the pipeline
        still runs and prints `[NEW]` lines, just nothing is notified).
      skills_root / roles_root / locations_root: the alias subtrees
        produced by `verification.match_profile.load_alias_files()`. Held
        by reference so we don't reload them per-job.
    """

    enabled_profiles: list[dict] = field(default_factory=list)
    skills_root: dict = field(default_factory=dict)
    roles_root: dict = field(default_factory=dict)
    locations_root: dict = field(default_factory=dict)


def load_config(path: Path) -> tuple[list[dict], dict]:
    """Load and parse the YAML config file. Returns (companies, platforms)."""
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping, got {type(data).__name__}")

    platforms = data.get("ats_platforms") or {}
    companies = data.get("companies") or []
    return companies, platforms


def _log_config_summary(companies: list[dict], platforms: dict) -> None:
    enabled = [c for c in companies if isinstance(c, dict) and c.get("enable")]
    print(
        f"Loaded config from {CONFIG_PATH.name}: "
        f"{len(platforms)} ATS platforms, {len(companies)} companies, "
        f"{len(enabled)} enabled.",
        flush=True,
    )


def _request_stop(signum, _frame) -> None:
    global _stop
    _stop = True
    print(f"\nReceived signal {signum}. Stopping after current tick...", flush=True)


def _run_telegram_sync_safely() -> int:
    """Sync Telegram-bot DMs into `chat_id` fields of `profiles.yaml`.

    This is the main-loop integration of `verification.sync_telegram_chat_ids`.
    It is wrapped in a hard "never raises" envelope: any failure path
    (missing `local_secrets`, missing/empty token, Telegram outage,
    HTTP timeout, malformed payload, …) is logged and swallowed so the
    main pipeline keeps running. Sync is a convenience, not a hard
    dependency of the matcher.

    Returns:
      The number of profile rows actually rewritten in profiles.yaml.
      Callers should treat any return > 0 as "the on-disk profiles
      changed; reload matching state next iteration" (see `main()` —
      this is done by flipping `_config_changed = True`).
    """
    # Imported locally so a missing `local_secrets.py` doesn't prevent the
    # rest of `main.py` from importing — sync becomes an opt-in feature.
    try:
        import local_secrets
    except ModuleNotFoundError:
        # First-run friendly: no warning storm, just a one-line breadcrumb.
        print(
            "  telegram sync: local_secrets.py not found at project root "
            "— skipping (set telegram_bot_token there to enable)",
            flush=True,
        )
        return 0

    token = getattr(local_secrets, "telegram_bot_token", None)
    if not isinstance(token, str) or not token.strip():
        print(
            "  telegram sync: telegram_bot_token is empty in "
            "local_secrets.py — skipping",
            flush=True,
        )
        return 0

    try:
        changes = sync_chat_ids(token, PROFILES_PATH)
    except SyncError as exc:
        # SyncError messages are token-scrubbed at the source (see
        # `fetch_updates` in sync_telegram_chat_ids.py).
        print(f"  telegram sync failed: {exc}", flush=True)
        return 0
    except Exception as exc:
        # Defensive catch — any unexpected exception must NOT take down
        # the main pipeline. We deliberately don't re-raise here.
        print(f"  telegram sync unexpected error: {exc!r}", flush=True)
        return 0

    if not changes:
        return 0

    for line in changes:
        print(f"  telegram sync:{line}", flush=True)
    return len(changes)


def init_db(path: Path) -> sqlite3.Connection:
    """Open the SQLite DB and ensure required tables exist."""
    # NOTE: sqlite3.connect accepts the constant DB_PATH (not user input),
    # so this does not violate the "no user input in file paths" rule.
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_last_fetch (
            slug TEXT PRIMARY KEY,
            last_fetch_ts REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ats_platform_last_fetch (
            platform TEXT PRIMARY KEY,
            last_fetch_ts REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ats_platform_rate_limit (
            platform TEXT PRIMARY KEY,
            cooldown_period REAL NOT NULL,
            last_rate_limited_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recent_job_ids (
            job_key TEXT PRIMARY KEY,
            fetched_at REAL NOT NULL
        )
        """
    )
    # Index speeds up the hourly cleanup (DELETE WHERE fetched_at < cutoff).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recent_job_ids_fetched_at "
        "ON recent_job_ids (fetched_at)"
    )
    conn.commit()
    return conn


def record_fetch(conn: sqlite3.Connection, slug: str) -> None:
    """Record (or update) the last fetch timestamp for a company."""
    conn.execute(
        """
        INSERT INTO company_last_fetch (slug, last_fetch_ts)
        VALUES (?, ?)
        ON CONFLICT(slug) DO UPDATE SET last_fetch_ts = excluded.last_fetch_ts
        """,
        (slug, time.time()),
    )
    conn.commit()


def record_platform_fetch(conn: sqlite3.Connection, platform: str) -> None:
    """Record (or update) the last fetch timestamp for an ATS platform."""
    conn.execute(
        """
        INSERT INTO ats_platform_last_fetch (platform, last_fetch_ts)
        VALUES (?, ?)
        ON CONFLICT(platform) DO UPDATE SET last_fetch_ts = excluded.last_fetch_ts
        """,
        (platform, time.time()),
    )
    conn.commit()


def record_rate_limit(conn: sqlite3.Connection, platform: str) -> None:
    """Record a rate-limit hit for an ATS platform.

    First hit (no row exists): cooldown_period = INITIAL_COOLDOWN_SECONDS (1h),
    last_rate_limited_at = now.
    Subsequent hits (row exists): cooldown_period doubles, last_rate_limited_at
    is updated to now.
    """
    now = time.time()
    conn.execute(
        """
        INSERT INTO ats_platform_rate_limit
            (platform, cooldown_period, last_rate_limited_at)
        VALUES (?, ?, ?)
        ON CONFLICT(platform) DO UPDATE SET
            cooldown_period = cooldown_period * 2,
            last_rate_limited_at = excluded.last_rate_limited_at
        """,
        (platform, INITIAL_COOLDOWN_SECONDS, now),
    )
    conn.commit()


def clear_rate_limit(conn: sqlite3.Connection, platform: str) -> None:
    """Clear any rate-limit state for an ATS platform (no-op if none exists)."""
    conn.execute(
        "DELETE FROM ats_platform_rate_limit WHERE platform = ?",
        (platform,),
    )
    conn.commit()


def record_job_id(conn: sqlite3.Connection, company_name: str, job_id: str) -> bool:
    """Record a fetched job in the 24-hour dedupe table.

    The key stored is ``f"{company_name}#{job_id}"``.

    Returns:
        True  if this job was ALREADY present (duplicate within the 24h window),
        False if it was newly inserted.
    """
    job_key = f"{company_name}#{job_id}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO recent_job_ids (job_key, fetched_at) VALUES (?, ?)",
        (job_key, time.time()),
    )
    conn.commit()
    # rowcount == 0 means the INSERT was IGNOREd => the row already existed.
    return cur.rowcount == 0


def cleanup_old_job_ids(conn: sqlite3.Connection) -> int:
    """Delete job IDs older than the retention window. Returns rows deleted."""
    cutoff = time.time() - JOB_ID_RETENTION_SECONDS
    cur = conn.execute(
        "DELETE FROM recent_job_ids WHERE fetched_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def _extract_job_id(job) -> str | None:
    """Best-effort: pull a stable per-job identifier off a jobhive job model.

    Used to build the `{company_slug}#{job_id}` key for the dedupe table.
    """
    for attr in ("id", "external_id", "job_id", "url", "apply_url"):
        value = getattr(job, attr, None)
        if value:
            return str(value)
    return None


def _extract_posted_at(job):
    """Best-effort: pull the posted/published timestamp off a jobhive job model.

    Different connectors use different field names, so we try a few common ones.
    Returns None if no posted-at-style field is present.
    """
    for attr in (
        "posted_at",
        "published_at",
        "date_posted",
        "posted_date",
        "created_at",
        "updated_at",
    ):
        value = getattr(job, attr, None)
        if value:
            return value
    return None


def _posted_at_to_ts(value) -> float | None:
    """Best-effort: convert a posted_at value into a Unix timestamp.

    Handles: datetime (naive treated as UTC), date, int/float (assumed seconds),
    ISO-8601 strings (with optional trailing 'Z'). Returns None if not parseable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, date):
        # date-only (not datetime); treat as start of day UTC.
        return datetime(
            value.year, value.month, value.day, tzinfo=timezone.utc
        ).timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def is_rate_limit_error(exc: Exception) -> bool:
    """Heuristic: detect rate-limit / 429 errors by inspecting the exception text.

    jobhive does not expose a typed rate-limit exception, so we sniff for the
    common markers in the error message.
    """
    text = str(exc).lower()
    return (
        "429" in text
        or "too many requests" in text
        or "rate limit" in text
        or "ratelimit" in text
    )


def fetch_live_jobs(ats: str, slug: str) -> dict:
    """Fetch jobs from the live ATS/source via jobhive's connector registry.

    Returns a result dict with shape:
        {"status": "success" | "rate_limited" | "failed",
         "jobs":   list[jobhive job model],   # empty unless status == "success"
         "error":  str | None}                # populated unless status == "success"
    """
    scraper = get_scraper(ats, slug)
    try:
        jobs = scraper.fetch()
        return {"status": "success", "jobs": jobs, "error": None}
    except Exception as exc:
        if is_rate_limit_error(exc):
            return {"status": "rate_limited", "jobs": [], "error": str(exc)}
        return {"status": "failed", "jobs": [], "error": str(exc)}


def _extract_jd_text(job) -> str:
    """Concatenate every JD-content attribute the job model exposes.

    Different jobhive connectors expose different fields (title is always
    present; description / content / body vary by ATS). We pull every
    candidate field in `JD_TEXT_ATTRS`, skip the missing/empty ones, and
    join the rest with newlines.

    The matcher's `normalize_jd` lowercases and collapses whitespace; HTML
    tags in description fields don't false-positive because all matching
    uses extended word boundaries (`[A-Za-z0-9_+#]`) so `<p>backend
    engineer</p>` matches `backend engineer` cleanly without leaking into
    the tag chars.

    Returns "" when the job has none of the expected fields populated — the
    caller treats that as "no signal, skip matching".
    """
    parts: list[str] = []
    for attr in JD_TEXT_ATTRS:
        value = getattr(job, attr, None)
        if value:
            parts.append(str(value))
    return "\n".join(parts)


def load_matching_state() -> MatchingState:
    """Load profiles + alias trees from disk and pre-filter to enabled profiles.

    Per context.md Rule 1 the caller is expected to have already invoked
    `validate_profiles(raise_on_error=True)` so an unresolved alias ref or
    a malformed rule won't reach this function. We re-raise whatever the
    underlying loaders raise (`FileNotFoundError`, `yaml.YAMLError`,
    `MatcherError`); the startup path turns those into a hard exit.
    """
    skills_root, roles_root, locations_root = load_alias_files()
    profiles = load_all_profiles()
    enabled = [
        p
        for p in profiles
        if isinstance(p, dict) and p.get("enable") is True
    ]
    return MatchingState(
        enabled_profiles=enabled,
        skills_root=skills_root,
        roles_root=roles_root,
        locations_root=locations_root,
    )


def _build_notification_text(profile_name: str, job) -> str:
    """Compose the Telegram message body for a matched (profile, job) pair.

    Returns a Telegram-HTML string. EVERY interpolated value is run
    through `html.escape` first — job titles, company names, locations
    and URLs all come from external job-board content and must be
    treated as untrusted (Secure Python Development rule #5 + #7).

    Layout (rendered in the Telegram chat):

        <b>JOB TITLE</b>
        Company: ACME
        Location: Remote
        Matched profile: <code>yaswanth_backend_distsys</code>
        Apply / view job  ← link (link preview card shown by Telegram)

    Missing fields are silently omitted rather than printed as "None".
    """
    title = getattr(job, "title", None) or "(no title)"
    company = (
        getattr(job, "company", None)
        or getattr(job, "company_name", None)
        or ""
    )
    location = getattr(job, "location", None) or ""
    url = getattr(job, "url", None) or getattr(job, "apply_url", None) or ""

    lines: list[str] = [f"<b>{html.escape(str(title))}</b>"]
    if company:
        lines.append(f"Company: {html.escape(str(company))}")
    if location:
        lines.append(f"Location: {html.escape(str(location))}")
    lines.append(
        f"Matched profile: <code>{html.escape(str(profile_name))}</code>"
    )
    if url:
        # Telegram requires a fully-qualified URL in href; we only emit
        # a clickable link when the connector actually provided one.
        # `html.escape(..., quote=True)` covers the href-attribute case.
        lines.append(
            f'<a href="{html.escape(str(url), quote=True)}">Apply / view job</a>'
        )
    return "\n".join(lines)


def notify(profile: dict, job, match_result: MatchResult) -> None:
    """Send a Telegram notification for one matched (profile, job) pair.

    The message goes to the `chat_id` of the matched profile, using the
    bot token from `local_secrets.telegram_bot_token`. The body
    contains the job title, company / location when available, the
    matched profile name, and a clickable link to the job posting
    (Telegram renders a preview card from the link).

    Graceful-degradation matrix (each step logs ONE breadcrumb and
    returns; the pipeline keeps running):
      * profile has no `chat_id` (owner hasn't DM'd the bot yet) → skip.
      * `local_secrets.py` is missing                              → skip.
      * `telegram_bot_token` is empty                              → skip.
      * Telegram `sendMessage` fails (network / API error)         → log + skip.

    The pre-existing `[MATCH]` console line is preserved unchanged so
    developers still get local visibility even when Telegram delivery
    is disabled (no token) or unavailable (network outage).

    This function is wrapped in a try/except by `_match_and_notify`,
    but we ALSO catch internally so that a Telegram outage shows up
    as a clear `notify:` breadcrumb rather than a generic
    `notify failed for profile ...` line.

    Args:
      profile: the matched profile dict (as parsed from profiles.yaml).
        Has at minimum `profile_name`, `chat_id` (possibly None / empty),
        `enable`, `years_of_experience`, `locations`, `matching_rules`.
      job: the jobhive job model that matched (`title`, usually
        `url` / `apply_url`, sometimes `company` / `location`).
      match_result: the full `MatchResult`. Currently unused by the
        notification body — kept in the signature because callers and
        tests pass it, and a future change may want to attach the
        per-rule scoring breakdown to the message.
    """
    profile_name = profile.get("profile_name") or "<unnamed>"
    title = getattr(job, "title", None)
    url = getattr(job, "url", None) or getattr(job, "apply_url", None)

    # Keep the local visibility line REGARDLESS of Telegram delivery so
    # console tail + log scrapes still see every match.
    print(
        f"      [MATCH] profile={profile_name} title={title!r} url={url}",
        flush=True,
    )

    chat_id = profile.get("chat_id")
    if not isinstance(chat_id, int):
        # `bool` is a subclass of int — exclude True/False as nonsense
        # chat_ids. Empty / None / str are all "owner hasn't bound yet".
        if isinstance(chat_id, bool) or chat_id is None or chat_id == "":
            print(
                f"      notify: profile {profile_name!r} has no chat_id "
                "(owner needs to DM the bot a `profile=<name>` message); "
                "Telegram delivery skipped",
                flush=True,
            )
        else:
            # Anything else (str with content, list, dict, ...) means the
            # YAML was edited by hand into a bad shape — surface that.
            print(
                f"      notify: profile {profile_name!r} has malformed "
                f"chat_id ({type(chat_id).__name__}); "
                "Telegram delivery skipped",
                flush=True,
            )
        return

    # Lazy import keeps `main.py` importable on a fresh checkout that
    # doesn't yet have `local_secrets.py` (the test suite depends on
    # this). Catch `ImportError` (parent of `ModuleNotFoundError`) so
    # both a missing file AND an `sys.modules[...] = None` test patch
    # are handled uniformly.
    try:
        import local_secrets  # noqa: WPS433 (intentional runtime import)
    except ImportError:
        print(
            "      notify: local_secrets.py not found at project root "
            "— Telegram delivery skipped",
            flush=True,
        )
        return

    token = getattr(local_secrets, "telegram_bot_token", None)
    if not isinstance(token, str) or not token.strip():
        print(
            "      notify: telegram_bot_token is empty in local_secrets.py "
            "— Telegram delivery skipped",
            flush=True,
        )
        return

    text = _build_notification_text(profile_name, job)

    try:
        send_message(token, chat_id, text)
    except SyncError as exc:
        # SyncError messages are token-scrubbed at the source.
        print(
            f"      notify: Telegram delivery failed for "
            f"profile {profile_name!r}: {exc}",
            flush=True,
        )
    except Exception as exc:
        # Defensive — any unexpected exception in the Telegram path must
        # not propagate. `_match_and_notify` would also catch this, but
        # we want the breadcrumb to read `notify:` not `notify failed`.
        print(
            f"      notify: Telegram delivery unexpected error for "
            f"profile {profile_name!r}: {exc!r}",
            flush=True,
        )


def _match_and_notify(job, matching_state: "MatchingState | None") -> None:
    """Evaluate `job` against every enabled profile; call `notify` per match.

    No-op when `matching_state` is None or has no enabled profiles —
    this is the common case when the user hasn't enabled any profile yet,
    and the pipeline should still run + dedupe + print `[NEW]` lines.

    Error handling is deliberately defensive:
      * Any `MatcherError` (or unexpected exception) from
        `match_profile_against_jd` is caught per-profile and logged. We
        do NOT abort the rest of the loop — one bad profile shouldn't
        silence matches from the other profiles.
      * Any exception from `notify` is caught for the same reason — the
        notify stub is going to be replaced with real I/O (Slack/HTTP)
        and a transient failure there shouldn't take down the tick.
    """
    if matching_state is None or not matching_state.enabled_profiles:
        return

    jd_text = _extract_jd_text(job)
    if not jd_text.strip():
        return

    for profile in matching_state.enabled_profiles:
        profile_name = profile.get("profile_name") or "<unnamed>"
        try:
            result = match_profile_against_jd(
                profile,
                jd_text,
                matching_state.skills_root,
                matching_state.roles_root,
                matching_state.locations_root,
            )
        except Exception as exc:
            # Catch-all (not just MatcherError) because a profile dict with
            # weird shapes could trigger AttributeError / TypeError inside
            # the matcher; we still don't want that to kill the tick.
            print(
                f"      match error for profile {profile_name!r}: {exc!r}",
                flush=True,
            )
            continue

        if not result.passed:
            continue

        try:
            notify(profile, job, result)
        except Exception as exc:
            # The notify stub is going to be replaced with real I/O; if a
            # future implementation throws (network error, etc.) we log
            # and move on rather than dropping subsequent profiles.
            print(
                f"      notify failed for profile {profile_name!r}: {exc!r}",
                flush=True,
            )


def process_fetch_result(
    conn: sqlite3.Connection,
    company: dict,
    result: dict,
    matching_state: "MatchingState | None" = None,
) -> None:
    """Apply DB writes for one fetch result. Runs in the main thread only.

    - rate_limited: record_rate_limit (sets/doubles cooldown). The cooldown
                    gate is stronger than delay_seconds, so the per-platform
                    last-fetch timestamps are intentionally NOT advanced here.
    - failed:       advance both the company and platform last-fetch timestamps
                    so the per-platform delay_seconds gate stops hammering a
                    broken endpoint, and oldest-first ordering moves on to
                    other companies. We do NOT clear the rate-limit cooldown.
    - success:      for each job, record `{slug}#{id}` in the dedupe table
                    (so we know if we've checked it before), print the job,
                    then update last-fetch timestamps and clear any cooldown.

    Jobs themselves are NOT persisted — they'll be handed off to a downstream
    consumer in a later step.
    """
    slug = str(company.get("slug") or "?")
    platform = str(company.get("ats_platform") or "?")
    status = result["status"]

    if status == "rate_limited":
        print(
            f"  fetching {slug} ({platform})... RATE LIMITED: {result['error']}",
            flush=True,
        )
        record_rate_limit(conn, platform)
        return

    if status == "failed":
        print(
            f"  fetching {slug} ({platform})... FAILED: {result['error']}",
            flush=True,
        )
        record_fetch(conn, slug)
        record_platform_fetch(conn, platform)
        return

    jobs = result["jobs"]

    # Drop jobs posted more than MAX_JOB_AGE_SECONDS ago. Jobs with no
    # parseable posted_at are kept (unknown age != known-to-be-old).
    cutoff_ts = time.time() - MAX_JOB_AGE_SECONDS
    too_old_count = 0
    fresh_jobs = []
    for job in jobs:
        posted_ts = _posted_at_to_ts(_extract_posted_at(job))
        if posted_ts is not None and posted_ts < cutoff_ts:
            too_old_count += 1
            continue
        fresh_jobs.append(job)

    new_count = 0
    repeat_count = 0
    skipped_count = 0
    for job in fresh_jobs:
        job_id = _extract_job_id(job)
        if not job_id:
            skipped_count += 1
            continue
        already_seen = record_job_id(conn, slug, job_id)
        if already_seen:
            repeat_count += 1
            # Repeats stay silent; they're still counted in the summary line.
            continue
        new_count += 1

        # Per-job [NEW] line silenced on purpose — the summary line below
        # (` ... ok (N jobs, K new, ...)`) carries the only count we want
        # routinely. Matched profiles still produce their own `[MATCH]`
        # line via `notify`, so notable jobs are NOT lost in the silence.
        # Uncomment the block below to re-enable per-job visibility.
        # title = getattr(job, "title", None)
        # location = getattr(job, "location", None)
        # url = getattr(job, "url", None) or getattr(job, "apply_url", None)
        # posted_at = _extract_posted_at(job)
        # print(
        #     f"    [NEW] {title} | {location} | posted={posted_at} | {url}",
        #     flush=True,
        # )
        _match_and_notify(job, matching_state)

    summary_bits = [f"{len(jobs)} jobs"]
    if too_old_count:
        summary_bits.append(f"{too_old_count} too old")
    summary_bits.append(f"{new_count} new")
    summary_bits.append(f"{repeat_count} repeat")
    if skipped_count:
        summary_bits.append(f"{skipped_count} skipped (no id)")
    print(
        f"  fetching {slug} ({platform})... ok ({', '.join(summary_bits)})",
        flush=True,
    )

    record_fetch(conn, slug)
    record_platform_fetch(conn, platform)
    clear_rate_limit(conn, platform)


def fetch_company(
    conn: sqlite3.Connection,
    company: dict,
    matching_state: "MatchingState | None" = None,
) -> None:
    """Sequential fetch + process for one company (kept for callers that don't
    want parallelism). The tick loop uses a thread pool directly, so this is
    not on the main hot path."""
    slug = str(company.get("slug") or "?")
    platform = str(company.get("ats_platform") or "?")
    result = fetch_live_jobs(platform, slug)
    process_fetch_result(conn, company, result, matching_state)


def is_platform_in_cooldown(conn: sqlite3.Connection, platform: str) -> bool:
    """True if the platform has an active rate-limit cooldown that hasn't elapsed.

    Returns False if no rate-limit row exists, or if (now >= last_rate_limited_at
    + cooldown_period).
    """
    row = conn.execute(
        "SELECT cooldown_period, last_rate_limited_at "
        "FROM ats_platform_rate_limit WHERE platform = ?",
        (platform,),
    ).fetchone()
    if row is None:
        return False
    cooldown_period, last_rate_limited_at = row
    return time.time() < (last_rate_limited_at + cooldown_period)


def get_platform_last_fetch_ts(
    conn: sqlite3.Connection, platform: str
) -> float | None:
    """Return last_fetch_ts for the platform, or None if it has never been fetched."""
    row = conn.execute(
        "SELECT last_fetch_ts FROM ats_platform_last_fetch WHERE platform = ?",
        (platform,),
    ).fetchone()
    return row[0] if row else None


def select_companies_to_fetch(
    conn: sqlite3.Connection,
    companies: list[dict],
    ats_platform: str,
    no_companies: int,
) -> list[dict]:
    """Pick up to `no_companies` enabled companies on `ats_platform`, ordered
    by oldest last fetch first.

    Companies that have NEVER been fetched (no row in `company_last_fetch`)
    get top priority, then by ascending `last_fetch_ts`.
    """
    if no_companies <= 0:
        return []

    platform_companies = [
        c
        for c in companies
        if isinstance(c, dict)
        and c.get("enable")
        and c.get("ats_platform") == ats_platform
        and c.get("slug")
    ]
    if not platform_companies:
        return []

    slugs = [str(c["slug"]) for c in platform_companies]
    # Safe: placeholders count is derived from our own data, not user input.
    placeholders = ",".join("?" * len(slugs))
    rows = conn.execute(
        f"SELECT slug, last_fetch_ts FROM company_last_fetch "
        f"WHERE slug IN ({placeholders})",
        slugs,
    ).fetchall()
    last_fetch_by_slug: dict[str, float] = {slug: ts for slug, ts in rows}

    def priority_key(c: dict) -> tuple[int, float]:
        ts = last_fetch_by_slug.get(str(c["slug"]))
        # Never fetched -> highest priority (sort group 0); else ascending ts.
        return (0, 0.0) if ts is None else (1, ts)

    platform_companies.sort(key=priority_key)
    return platform_companies[:no_companies]


def tick(
    tick_number: int,
    companies: list[dict],
    platforms: dict,
    db_conn: sqlite3.Connection,
    matching_state: "MatchingState | None" = None,
) -> None:
    """Single iteration of the pipeline.

    For each ATS platform:
      1. Skip if it's currently in a rate-limit cooldown.
      2. Skip if its last platform-level fetch was within `delay_seconds`.
      3. Otherwise, select up to `max_concurrency` companies (oldest fetch
         first) and fetch them IN PARALLEL via a per-platform thread pool of
         size `max_concurrency`. HTTP fetches run on worker threads; all DB
         writes (process_fetch_result) run on the main thread to keep the
         single SQLite connection thread-safe.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    enabled = [c for c in companies if isinstance(c, dict) and c.get("enable")]
    print(
        f"[{timestamp}] tick #{tick_number} - "
        f"{len(enabled)} enabled / {len(companies)} companies, "
        f"{len(platforms)} platforms",
        flush=True,
    )

    now = time.time()
    for platform_name, platform_cfg in platforms.items():
        if not isinstance(platform_cfg, dict):
            continue

        # Check 1: not currently in a rate-limit cooldown.
        if is_platform_in_cooldown(db_conn, platform_name):
            continue

        # Check 2: per-platform delay since last successful fetch.
        delay_seconds = float(platform_cfg.get("delay_seconds", 60.0))
        last_ts = get_platform_last_fetch_ts(db_conn, platform_name)
        if last_ts is not None and (now - last_ts) < delay_seconds:
            continue

        # Both checks passed: pick the companies most overdue for a fetch.
        max_concurrency = int(platform_cfg.get("max_concurrency", 1))
        chosen = select_companies_to_fetch(
            db_conn, companies, platform_name, max_concurrency
        )
        if not chosen:
            continue

        # Workers do pure I/O (no DB). Process results in the main thread.
        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            future_to_company = {
                pool.submit(
                    fetch_live_jobs,
                    platform_name,
                    str(company.get("slug") or "?"),
                ): company
                for company in chosen
            }
            for fut in as_completed(future_to_company):
                company = future_to_company[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    # fetch_live_jobs catches its own exceptions, so this is
                    # defensive (e.g., for unexpected pool errors).
                    result = {"status": "failed", "jobs": [], "error": str(exc)}
                process_fetch_result(db_conn, company, result, matching_state)

def main() -> int:
    global _config_changed

    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _request_stop)
        except (OSError, ValueError):
            pass

    print(
        f"Starting YPK Job Scraper. Ticking every {TICK_INTERVAL_SECONDS}s. "
        "Press Ctrl+C to stop.",
        flush=True,
    )

    _config_changed = True
    companies: list[dict] = []
    platforms: dict = {}
    matching_state: MatchingState | None = None

    db_conn = init_db(DB_PATH)
    print(f"Opened SQLite DB at {DB_PATH.name}", flush=True)

    last_cleanup_at = time.monotonic()
    # 0.0 (not monotonic()) so the first tick triggers a Telegram sync
    # immediately — that way startup picks up any chat_id binding the
    # owner sent before launching `main.py`. If the token is missing,
    # `_run_telegram_sync_safely` returns 0 and logs a one-liner.
    last_telegram_sync_at = 0.0

    try:
        tick_number = 0
        while not _stop:
            tick_number += 1
            started = time.monotonic()

            # Sync Telegram-driven chat_id bindings before validating /
            # loading matching state — so any changes the sync wrote are
            # picked up by this same iteration's reload below. The sync
            # itself never raises (see `_run_telegram_sync_safely`).
            if started - last_telegram_sync_at >= TELEGRAM_SYNC_INTERVAL_SECONDS:
                applied = _run_telegram_sync_safely()
                last_telegram_sync_at = started
                if applied > 0:
                    # Force the reload block to re-validate profiles
                    # (Rule 1) and rebuild matching_state with the new
                    # chat_id values. This is the same mechanism that
                    # picks up an external edit to profiles.yaml.
                    _config_changed = True

            if _config_changed:
                try:
                    companies, platforms = load_config(CONFIG_PATH)
                except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
                    print(f"Failed to load config: {exc}", file=sys.stderr, flush=True)
                    return 1
                _log_config_summary(companies, platforms)

                # Per context.md "Rule 1 — Validate profiles after EVERY edit":
                # validate profiles/aliases BEFORE loading matching state,
                # and refuse to start if validation fails. A typo here would
                # silently degrade matching, which is exactly the failure
                # mode this project cannot tolerate.
                try:
                    validate_profiles(raise_on_error=True)
                except ValidationError as exc:
                    print(
                        "profile/alias validation failed; refusing to start:\n"
                        f"{exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1

                try:
                    matching_state = load_matching_state()
                except (MatcherError, FileNotFoundError, yaml.YAMLError) as exc:
                    print(
                        f"failed to load matching state: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1

                n_enabled = len(matching_state.enabled_profiles)
                if n_enabled == 0:
                    print(
                        "Loaded matching state: 0 enabled profile(s) — "
                        "matching is a no-op until a profile is enabled.",
                        flush=True,
                    )
                else:
                    names = ", ".join(
                        repr(p.get("profile_name"))
                        for p in matching_state.enabled_profiles
                    )
                    print(
                        f"Loaded matching state: {n_enabled} enabled profile(s): "
                        f"{names}",
                        flush=True,
                    )
                _config_changed = False

            # Hourly: drop job IDs older than the 24-hour retention window.
            if started - last_cleanup_at >= CLEANUP_INTERVAL_SECONDS:
                try:
                    deleted = cleanup_old_job_ids(db_conn)
                    print(
                        f"  cleanup: removed {deleted} job id(s) older than 24h",
                        flush=True,
                    )
                except sqlite3.Error as exc:
                    print(f"cleanup failed: {exc!r}", flush=True)
                last_cleanup_at = started

            try:
                tick(tick_number, companies, platforms, db_conn, matching_state)
            except Exception as exc:
                print(f"tick #{tick_number} failed: {exc!r}", flush=True)

            elapsed = time.monotonic() - started
            remaining = max(0.0, TICK_INTERVAL_SECONDS - elapsed)
            # Sleep in small slices so Ctrl+C is responsive.
            slept = 0.0
            while slept < remaining and not _stop:
                step = min(0.5, remaining - slept)
                time.sleep(step)
                slept += step
    finally:
        db_conn.close()

    print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
