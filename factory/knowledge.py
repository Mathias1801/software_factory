"""Cross-project learning without extra model calls.

Lessons are written into each project's own factory_state/lessons.jsonl (so everything stays in the
project's master folder) and read back from ALL sibling projects in the workspace. A lesson is
recorded when a task only passed after retries, or when the human rejected a proposed decision.
Retrieval is plain keyword overlap: zero tokens to find, a few hundred tokens to include."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from factory.artifacts import ArtifactStore

_WORD = re.compile(r"[a-z0-9_]{4,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class Knowledge:
    def __init__(self, store: ArtifactStore):
        self.store = store
        self.workspace = store.root.parent

    def _read_all(self, filename: str) -> list[dict]:
        rows = []
        for path in sorted(self.workspace.glob(f"*/factory_state/{filename}")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return rows

    def record_lesson(self, *, problem: str, fix: str, tags: list[str], context: str = "") -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "project": self.store.root.name,
            "tags": sorted(set(tags)),
            "keywords": sorted(_words(" ".join([problem, context, *tags])))[:40],
            "problem": problem[:400],
            "fix": fix[:400],
        }
        self.store.append_text("lessons.jsonl", json.dumps(row, ensure_ascii=False) + "\n")

    def lessons_for(self, text: str, tags: list[str], limit: int = 3, max_chars: int = 1500) -> str:
        wanted = _words(text) | set(tags)
        scored = []
        for lesson in self._read_all("lessons.jsonl"):
            score = len(wanted & (set(lesson.get("keywords", [])) | set(lesson.get("tags", []))))
            if score:
                scored.append((score, lesson.get("ts", ""), lesson))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        lines, used = [], 0
        for _, _, lesson in scored[:limit]:
            line = f"- Problem: {lesson['problem']} | What worked: {lesson['fix']}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def avg_task_tokens(self) -> tuple[float | None, int]:
        spent = []
        for path in sorted(self.workspace.glob("*/factory_state/telemetry.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if '"task_result"' in line:
                    row = json.loads(line)
                    if row.get("outcome") == "passed":
                        spent.append(int(row.get("tokens_spent", 0)))
        return (sum(spent) / len(spent), len(spent)) if spent else (None, 0)


def workspace_of(project_dir: str | Path) -> Path:
    return Path(project_dir).expanduser().parent
