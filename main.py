"""YPK Job Scraper - entry point.

Runs a continuous loop that calls `tick()` every 5 seconds.
This is the minimal skeleton; pipeline stages will be added incrementally.
See context.md for the overall architecture and development style.
"""

from __future__ import annotations

import signal
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from jobhive.scrapers import get_scraper

TICK_INTERVAL_SECONDS = 5
CONFIG_PATH = Path(__file__).parent / "config.yml"
DB_PATH = Path(__file__).parent / "pipeline_state.db"
INITIAL_COOLDOWN_SECONDS = 60 * 60  # 1 hour
JOB_ID_RETENTION_SECONDS = 24 * 60 * 60  # 24 hours
CLEANUP_INTERVAL_SECONDS = 60 * 60  # 1 hour
MAX_JOB_AGE_SECONDS = 30 * 24 * 60 * 60  # drop jobs posted more than 24h ago

_config_changed = False
_stop = False


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


def process_fetch_result(
    conn: sqlite3.Connection, company: dict, result: dict
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
        else:
            new_count += 1

        title = getattr(job, "title", None)
        location = getattr(job, "location", None)
        url = getattr(job, "url", None) or getattr(job, "apply_url", None)
        posted_at = _extract_posted_at(job)
        marker = "REPEAT" if already_seen else "NEW   "
        print(
            f"    [{marker}] {title} | {location} | posted={posted_at} | {url}",
            flush=True,
        )

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


def fetch_company(conn: sqlite3.Connection, company: dict) -> None:
    """Sequential fetch + process for one company (kept for callers that don't
    want parallelism). The tick loop uses a thread pool directly, so this is
    not on the main hot path."""
    slug = str(company.get("slug") or "?")
    platform = str(company.get("ats_platform") or "?")
    result = fetch_live_jobs(platform, slug)
    process_fetch_result(conn, company, result)


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
                process_fetch_result(db_conn, company, result)

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

    db_conn = init_db(DB_PATH)
    print(f"Opened SQLite DB at {DB_PATH.name}", flush=True)

    last_cleanup_at = time.monotonic()

    try:
        tick_number = 0
        while not _stop:
            tick_number += 1
            started = time.monotonic()
            if _config_changed:
                try:
                    companies, platforms = load_config(CONFIG_PATH)
                except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
                    print(f"Failed to load config: {exc}", file=sys.stderr, flush=True)
                    return 1
                _log_config_summary(companies, platforms)
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
                tick(tick_number, companies, platforms, db_conn)
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
