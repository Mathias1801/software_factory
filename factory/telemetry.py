"""Append-only JSONL event log. Feeds the dashboard and, later, a learned router."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class Telemetry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def read(self, event: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if event is None or row.get("event") == event:
                rows.append(row)
        return rows
