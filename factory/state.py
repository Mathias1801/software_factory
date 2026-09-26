"""Spec, task and run-state models. Persisted as JSON artifacts so runs can resume."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from factory.artifacts import ArtifactStore


def _str_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    acceptance_ids: list[str] = field(default_factory=list)
    domain_tags: list[str] = field(default_factory=list)
    risk_tags: list[str] = field(default_factory=list)
    files_hint: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict, index: int = 1) -> Task:
        return cls(
            id=str(d.get("id") or f"T{index}"),
            title=str(d.get("title", "")),
            description=str(d.get("description", "")),
            acceptance_ids=_str_list(d.get("acceptance_ids")),
            domain_tags=[t.lower() for t in _str_list(d.get("domain_tags"))],
            risk_tags=[t.lower() for t in _str_list(d.get("risk_tags"))],
            files_hint=_str_list(d.get("files_hint")),
        )


@dataclass
class Spec:
    summary: str
    in_scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    acceptance_criteria: list[dict] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    tech_stack: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> Spec:
        criteria = []
        for i, c in enumerate(d.get("acceptance_criteria") or [], start=1):
            if isinstance(c, dict):
                criteria.append({"id": str(c.get("id") or f"AC{i}"), "text": str(c.get("text", ""))})
            else:
                criteria.append({"id": f"AC{i}", "text": str(c)})
        return cls(
            summary=str(d.get("summary", "")),
            in_scope=_str_list(d.get("in_scope")),
            out_of_scope=_str_list(d.get("out_of_scope")),
            assumptions=_str_list(d.get("assumptions")),
            open_questions=_str_list(d.get("open_questions")),
            acceptance_criteria=criteria,
            tasks=[Task.from_dict(t, i) for i, t in enumerate(d.get("tasks") or [], start=1)],
            tech_stack=dict(d.get("tech_stack") or {}),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def criteria_for(self, task: Task) -> list[dict]:
        if not task.acceptance_ids:
            return self.acceptance_criteria
        wanted = set(task.acceptance_ids)
        return [c for c in self.acceptance_criteria if c["id"] in wanted]

    def to_markdown(self) -> str:
        def bullets(items):
            return "\n".join(f"- {i}" for i in items) or "- (none)"

        tasks = "\n".join(
            f"- **{t.id}** {t.title} (criteria: {', '.join(t.acceptance_ids) or 'all'}; "
            f"tags: {', '.join(t.domain_tags + t.risk_tags) or 'none'})"
            for t in self.tasks
        )
        criteria = "\n".join(f"- **{c['id']}** {c['text']}" for c in self.acceptance_criteria)
        stack = "\n".join(f"- {k}: {v}" for k, v in self.tech_stack.items()) or "- (not specified)"
        return (
            f"# Spec\n\n{self.summary}\n\n## In scope\n{bullets(self.in_scope)}\n\n"
            f"## Out of scope\n{bullets(self.out_of_scope)}\n\n## Assumptions\n{bullets(self.assumptions)}\n\n"
            f"## Open questions\n{bullets(self.open_questions)}\n\n## Acceptance criteria\n{criteria}\n\n"
            f"## Tech stack\n{stack}\n\n## Tasks\n{tasks}\n"
        )


@dataclass
class RunState:
    mode: str | None = None
    spec_done: bool = False
    acceptance_done: bool = False
    completed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    integration_passed: bool | None = None
    handoff_done: bool = False
    deployed: bool = False

    @classmethod
    def load(cls, store: ArtifactStore) -> RunState:
        data = store.read_json("state.json") or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self, store: ArtifactStore) -> None:
        store.write_json("state.json", asdict(self))
