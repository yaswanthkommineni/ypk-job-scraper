"""Standalone fetch tester — no DB, no loop, no rate limiting.

Use this to verify a single (ats, slug) pair, debug suspected wrong slugs,
inspect what jobhive actually returns, or sanity-check rate-limit/error paths
without bringing up the full pipeline.

Usage:
    python try_fetch.py <ats> <slug>                # fetch one
    python try_fetch.py <ats> <slug> --limit 5      # only print first 5 jobs
    python try_fetch.py <ats> <slug> --raw          # also dump model_dump JSON
    python try_fetch.py --examples                  # try a handful of known slugs

Examples:
    python try_fetch.py greenhouse swiggy
    python try_fetch.py lever atlassian
    python try_fetch.py ashby openai
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback

from main import _extract_job_id, _extract_posted_at, fetch_live_jobs

# A small spread across ATSes for quick smoke tests.
EXAMPLES: list[tuple[str, str]] = [
    ("greenhouse", "swiggy"),
    ("greenhouse", "razorpay"),
    ("lever", "atlassian"),
    ("ashby", "openai"),
    ("ashby", "notion"),
]


def print_job(idx: int, job, dump_raw: bool) -> None:
    job_id = _extract_job_id(job)
    title = getattr(job, "title", None)
    location = getattr(job, "location", None)
    url = getattr(job, "url", None) or getattr(job, "apply_url", None)
    posted_at = _extract_posted_at(job)
    print(f"  [{idx}] id={job_id}")
    print(f"      title:    {title}")
    print(f"      location: {location}")
    print(f"      posted:   {posted_at}")
    print(f"      url:      {url}")
    if dump_raw:
        try:
            data = job.model_dump(mode="json")
        except Exception as exc:
            data = {"_dump_failed": str(exc), "_repr": repr(job)}
        print("      raw:", json.dumps(data, indent=2, default=str))


def try_one(ats: str, slug: str, limit: int, dump_raw: bool) -> int:
    print(f"\n=== {ats}:{slug} ===", flush=True)
    try:
        result = fetch_live_jobs(ats, slug)
    except Exception as exc:
        # fetch_live_jobs catches its own exceptions, so this is defensive.
        print(f"  unexpected: {exc!r}")
        traceback.print_exc()
        return 1

    print(f"  status: {result['status']}")
    if result.get("error"):
        print(f"  error:  {result['error']}")

    jobs = result.get("jobs") or []
    print(f"  jobs:   {len(jobs)}")
    for idx, job in enumerate(jobs[:limit], 1):
        print_job(idx, job, dump_raw)

    return 0 if result["status"] == "success" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Quick standalone tester for a single ATS slug fetch."
    )
    parser.add_argument("ats", nargs="?", help="ATS platform (e.g. greenhouse, lever, ashby)")
    parser.add_argument("slug", nargs="?", help="Company slug on that ATS")
    parser.add_argument(
        "--limit", type=int, default=10, help="Max jobs to print (default: 10)"
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Also dump each job's full model_dump JSON",
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="Run a small built-in set of (ats, slug) pairs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.examples:
        any_failed = False
        for ats, slug in EXAMPLES:
            rc = try_one(ats, slug, args.limit, args.raw)
            any_failed = any_failed or (rc != 0)
        return 1 if any_failed else 0

    if not args.ats or not args.slug:
        print("error: provide both <ats> and <slug>, or use --examples", file=sys.stderr)
        return 2

    return try_one(args.ats, args.slug, args.limit, args.raw)


if __name__ == "__main__":
    sys.exit(main())
