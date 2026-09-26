"""Decision policy and log. Cheap, reversible choices are logged; costly or
scope-changing ones need human approval depending on the autonomy level."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum

from factory.artifacts import ArtifactStore


class Reversibility(Enum):
    CHEAP = "cheap"
    COSTLY = "costly"


@dataclass
class Decision:
    title: str
    options: list[str]
    chosen: str | None
    rationale: str
    reversibility: Reversibility
    category: str = "implementation"  # implementation | dependency | architecture | scope_change
    touches_spec_scope: bool = False
    risk_tags: set[str] = field(default_factory=set)

    @classmethod
    def from_dict(cls, d: dict) -> Decision:
        rev = str(d.get("reversibility", "cheap")).lower()
        return cls(
            title=str(d.get("title", "untitled decision")),
            options=[str(o) for o in d.get("options") or []],
            chosen=d.get("chosen"),
            rationale=str(d.get("rationale", "")),
            reversibility=Reversibility.COSTLY if rev == "costly" else Reversibility.CHEAP,
            category=str(d.get("category", "implementation")),
            touches_spec_scope=bool(d.get("touches_spec_scope", False)),
            risk_tags={str(t) for t in d.get("risk_tags") or []},
        )

    def describe(self) -> str:
        return (
            f"Options: {', '.join(self.options) or 'n/a'}\n"
            f"Proposed: {self.chosen}\n"
            f"Why: {self.rationale}\n"
            f"Reversibility: {self.reversibility.value} | Category: {self.category}\n"
            f"Risk tags: {', '.join(sorted(self.risk_tags)) or 'none'}"
        )


def needs_approval(d: Decision, profile: dict) -> bool:
    policy = profile["human"]
    if d.touches_spec_scope or d.category == "scope_change":
        return True
    if d.category in policy.get("approval_required_for", []):
        return True
    if d.risk_tags & set(policy.get("always_ask_on", [])):
        return True
    autonomy = policy.get("autonomy", "medium")
    if d.reversibility is Reversibility.COSTLY:
        return autonomy != "high"
    if autonomy == "low" and d.category != "implementation":
        return True
    return False


class DecisionLog:
    def __init__(self, store: ArtifactStore):
        self.store = store

    def record(self, d: Decision, approved: bool, by: str, task_id: str = "", feedback: str = "") -> None:
        row = asdict(d)
        row["reversibility"] = d.reversibility.value
        row["risk_tags"] = sorted(d.risk_tags)
        row.update(
            approved=approved,
            decided_by=by,
            task_id=task_id,
            feedback=feedback,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.store.append_text("decisions.jsonl", json.dumps(row, ensure_ascii=False) + "\n")
        status = "approved" if approved else "REJECTED"
        self.store.append_text(
            "decisions.md",
            f"- [{task_id or '-'}] {d.title}: {d.chosen} ({d.reversibility.value}, {status} by {by}). {d.rationale}\n",
        )
