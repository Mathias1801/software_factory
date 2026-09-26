"""The project master folder. Everything a run creates lives in one folder:

    <workspace>/<project>/
      PROJECT.md        overview: status, how to run, where things are (always written)
      dashboard.html    only when outputs.dashboard is on
      app/              the software being built (the deliverable)
      docs/             human-readable records: idea, interview, spec, decisions, feedback, tests
      factory_state/    machine state: spec.json, state, ledger, telemetry, tool policy, profile used

Files are routed by name: *.md -> docs/, *.html and PROJECT.md -> root, everything else -> factory_state/."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT_FILES = {"PROJECT.md", "dashboard.html"}


class ArtifactStore:
    def __init__(self, project_dir: str | Path):
        self.root = Path(project_dir).expanduser()
        self.app = self.root / "app"
        self.docs = self.root / "docs"
        self.state_dir = self.root / "factory_state"
        for d in (self.app, self.docs, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def repo(self) -> Path:  # alias kept for readability in check/tool code
        return self.app

    def path(self, name: str) -> Path:
        if name in ROOT_FILES or name.endswith(".html"):
            return self.root / name
        if name.endswith(".md"):
            return self.docs / name
        return self.state_dir / name

    def write_json(self, name: str, data: Any) -> Path:
        p = self.path(name)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    def read_json(self, name: str, default: Any = None) -> Any:
        p = self.path(name)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    def write_text(self, name: str, text: str) -> Path:
        p = self.path(name)
        p.write_text(text, encoding="utf-8")
        return p

    def read_text(self, name: str, default: str = "") -> str:
        p = self.path(name)
        return p.read_text(encoding="utf-8") if p.exists() else default

    def append_text(self, name: str, text: str) -> None:
        with self.path(name).open("a", encoding="utf-8") as fh:
            fh.write(text)
