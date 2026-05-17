"""Print last 10 entries from the dedup DB and exit. Useful for debugging."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the script runnable directly: `python3 services/notifier/healthcheck.py`
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.notifier.dedup import DedupStore
from services.notifier.main import default_db_path


def main() -> int:
    path = default_db_path()
    if not path.exists():
        print(f"db not found at {path}")
        return 0
    with DedupStore(path) as store:
        total = store.count()
        rows = store.recent(10)
        print(f"db: {path}")
        print(f"total seen jobs: {total}")
        print(f"last {len(rows)} entries:")
        for first_seen_at, company, title, score, notified, url in rows:
            tag = "sent" if notified else "queued"
            print(
                f"  [{first_seen_at}] {tag:6s} score={score:.2f} "
                f"{company} — {title}\n    {url}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
