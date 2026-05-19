"""One-shot maintenance: clear `recent_job_ids` without dropping the table.

Run from project root:
    python verification/clear_recent_job_ids.py

This empties the dedupe table so the pipeline will treat every job it
sees on the next tick as NEW (and therefore evaluate every enabled
profile against it again). The table itself, its indexes, and every
other table in `pipeline_state.db` are left untouched.

This is a developer tool, not a pipeline component — don't import it
from `main.py`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Project-root resolution mirrors every other script in `verification/`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "pipeline_state.db"


def main() -> int:
    if not DB_PATH.is_file():
        # No DB yet -> nothing to clear; treat as success so this script
        # is safe to run on a fresh checkout.
        print(f"no db at {DB_PATH} — nothing to clear")
        return 0

    # DB_PATH is a project-relative constant (not user input), so this
    # does not violate the "no user input in file paths" rule.
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        # Verify the table exists before we try to count / delete; we
        # never CREATE / DROP it here — that's main.init_db's job.
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='recent_job_ids'"
        ).fetchone()
        if row is None:
            print(
                "table `recent_job_ids` not found — start main.py once "
                "to let init_db create the schema, then re-run this"
            )
            return 1

        before = cur.execute("SELECT COUNT(*) FROM recent_job_ids").fetchone()[0]
        cur.execute("DELETE FROM recent_job_ids")
        conn.commit()
        after = cur.execute("SELECT COUNT(*) FROM recent_job_ids").fetchone()[0]
        tables = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()

    print(f"recent_job_ids cleared: {before} -> {after} rows")
    print(f"tables still present:   {tables}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
