"""One-shot auditor: hit every (ats, slug) from config.yml and report results.

Run:  python audit_slugs.py
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from main import fetch_live_jobs

CONFIG_PATH = Path(__file__).parent / "config.yml"
WORKERS = 8


def load_companies() -> list[dict]:
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return list(data.get("companies") or [])


def audit_one(company: dict) -> dict:
    ats = str(company.get("ats_platform") or "?")
    slug = str(company.get("slug") or "?")
    name = str(company.get("company_name") or slug)
    enable = bool(company.get("enable"))
    t0 = time.monotonic()
    try:
        result = fetch_live_jobs(ats, slug)
    except Exception as exc:
        result = {"status": "failed", "jobs": [], "error": f"raised: {exc!r}"}
    elapsed = time.monotonic() - t0
    return {
        "ats": ats,
        "slug": slug,
        "name": name,
        "enable": enable,
        "status": result["status"],
        "n_jobs": len(result.get("jobs") or []),
        "error": (result.get("error") or "")[:200],
        "elapsed": elapsed,
    }


def main() -> int:
    companies = load_companies()
    print(f"Auditing {len(companies)} entries from config.yml with {WORKERS} workers...\n", flush=True)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        future_to_company = {pool.submit(audit_one, c): c for c in companies}
        for fut in as_completed(future_to_company):
            r = fut.result()
            marker = {"success": "OK  ", "rate_limited": "RATE", "failed": "FAIL"}.get(r["status"], "????")
            line = f"  [{marker}] {r['ats']:>16}:{r['slug']:<20} {r['n_jobs']:>5} jobs  ({r['elapsed']:.1f}s)"
            if r["status"] != "success":
                line += f"  -- {r['error']}"
            print(line, flush=True)
            results.append(r)

    print("\n" + "=" * 80)
    print("SUMMARY by status")
    print("=" * 80)
    for status in ("success", "rate_limited", "failed"):
        bucket = [r for r in results if r["status"] == status]
        bucket.sort(key=lambda r: (r["ats"], r["slug"]))
        print(f"\n{status.upper()} ({len(bucket)}):")
        for r in bucket:
            extra = ""
            if status == "success":
                extra = f"  {r['n_jobs']} jobs"
            elif r["error"]:
                extra = f"  -- {r['error']}"
            print(f"  {r['ats']:>16}:{r['slug']:<20}{extra}")

    print("\n" + "=" * 80)
    print(f"TOTAL: {len(results)} entries | "
          f"OK: {sum(1 for r in results if r['status']=='success')} | "
          f"RATE_LIMITED: {sum(1 for r in results if r['status']=='rate_limited')} | "
          f"FAILED: {sum(1 for r in results if r['status']=='failed')}")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
