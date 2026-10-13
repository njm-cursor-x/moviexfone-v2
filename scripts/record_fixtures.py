"""Try to record small live fixtures. Exits 0 on success, 2 if the network blocks the hosts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from moviexfone.sources.fetch import ALAMO_URL, NITEHAWK_URLS, fetch_alamo, fetch_nitehawk


def main() -> int:
    out = Path("data/fixtures/live")
    out.mkdir(parents=True, exist_ok=True)
    try:
        rows = fetch_alamo()
    except Exception as exc:
        print(f"blocked: Alamo {ALAMO_URL} ({type(exc).__name__}: {exc})")
        print("Assemble and Nitehawk are on the same restricted path from some networks.")
        print("The repo already contains synthetic fixtures dated 2026-10-13.")
        return 2
    (out / "alamo_parsed.json").write_text(
        json.dumps([row.__dict__ | {"starts_at": row.starts_at.isoformat()} for row in rows[:30]], indent=2),
        encoding="utf-8",
    )
    print(f"alamo sessions parsed: {len(rows)}")
    try:
        nitehawk = fetch_nitehawk()
        print(f"nitehawk screenings parsed: {len(nitehawk)} from {list(NITEHAWK_URLS)}")
    except Exception as exc:
        print(f"nitehawk blocked: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
