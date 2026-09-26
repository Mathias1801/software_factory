"""Human-in-the-loop. Blocking requests stop work until answered; non-blocking
requests proceed on a stated assumption and are reviewed in batches."""
from __future__ import annotations

import textwrap
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from uuid import uuid4

from factory.telemetry import Telemetry

YES = {"y", "yes", "approve", "approved", "a", "ok", "continue"}
ACCEPT = {"", "y", "yes", "ok", "accept", "fine"}


@dataclass
class HumanRequest:
    kind: str  # question | approval | readback | tool_suggestion | mode | spec_issue
    stage: str
    summary: str
    context: str = ""
    options: list[str] = field(default_factory=list)
    blocking: bool = True
    default_assumption: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    answer: str | None = None

    def __post_init__(self):
        if not self.blocking and not self.default_assumption:
            raise ValueError("Non-blocking requests must state the assumption used")


class HumanChannel(ABC):
    @abstractmethod
    def ask(self, req: HumanRequest) -> str: ...

    def ask_batch(self, reqs: list[HumanRequest]) -> list[str]:
        return [self.ask(r) for r in reqs]


class CLIHumanChannel(HumanChannel):
    def ask(self, req: HumanRequest) -> str:
        tag = "BLOCKING" if req.blocking else "non-blocking"
        print(f"\n[{req.stage.upper()}] {req.kind} ({tag})")
        print(req.summary)
        if req.context:
            print(textwrap.indent(req.context.strip(), "  | "))
        for i, option in enumerate(req.options, start=1):
            print(f"  {i}. {option}")
        if req.default_assumption:
            print(f"  Press Enter to accept the assumption: {req.default_assumption}")
        while True:
            ans = input("> ").strip()
            if req.options and ans.isdigit() and 1 <= int(ans) <= len(req.options):
                return req.options[int(ans) - 1]
            if ans or not req.blocking:
                return ans
            print("  An answer is needed here (type 'skip' to let the agent assume).")

    def ask_batch(self, reqs: list[HumanRequest]) -> list[str]:
        if len(reqs) > 1:
            print(f"\n=== {len(reqs)} items from {reqs[0].stage} ===")
        return [self.ask(r) for r in reqs]


class ScriptedHumanChannel(HumanChannel):
    """For dry runs and tests. Pops scripted answers, then falls back to defaults per kind."""

    DEFAULTS = {
        "approval": "approve",
        "readback": "y",
        "question": "No strong preference, use your best judgement.",
        "tool_suggestion": "not now",
    }

    def __init__(self, answers: list[str] | None = None):
        self.answers = list(answers or [])
        self.asked: list[HumanRequest] = []

    def ask(self, req: HumanRequest) -> str:
        self.asked.append(req)
        if self.answers:
            return self.answers.pop(0)
        if not req.blocking:
            return ""
        return self.DEFAULTS.get(req.kind, "y")


class HumanLoop:
    def __init__(self, channel: HumanChannel, telemetry: Telemetry):
        self.channel = channel
        self.telemetry = telemetry
        self.queue: list[HumanRequest] = []

    def _log(self, req: HumanRequest) -> None:
        self.telemetry.log(
            "human_request", kind=req.kind, stage=req.stage, blocking=req.blocking, summary=req.summary[:200]
        )

    def ask(self, req: HumanRequest) -> str:
        if not req.blocking:
            self.queue.append(req)
            self.telemetry.log("assumption_made", stage=req.stage, assumption=req.default_assumption)
            return req.default_assumption or ""
        self._log(req)
        req.answer = self.channel.ask(req)
        return req.answer

    def ask_batch(self, reqs: list[HumanRequest]) -> list[str]:
        for r in reqs:
            self._log(r)
        answers = self.channel.ask_batch(reqs)
        for r, a in zip(reqs, answers, strict=True):
            r.answer = a
        return answers

    def approve(self, stage: str, summary: str, context: str = "") -> tuple[bool, str]:
        req = HumanRequest(kind="approval", stage=stage, summary=summary, context=context, options=["approve", "reject"])
        ans = self.ask(req).strip()
        approved = ans.lower() in YES
        feedback = "" if approved else ans
        if not approved and ans.lower() in {"reject", "r", "n", "no"}:
            feedback = self.ask(
                HumanRequest(kind="question", stage=stage, summary="What should change?", blocking=True)
            )
        self.telemetry.log("approval", stage=stage, approved=approved, summary=summary[:200])
        return approved, feedback

    def flush(self, stage: str | None = None) -> list[HumanRequest]:
        """Review queued assumptions in one batch. Returns the ones the human overruled."""
        pending = [r for r in self.queue if stage is None or r.stage == stage]
        if not pending:
            return []
        self.queue = [r for r in self.queue if r not in pending]
        answers = self.ask_batch(pending)
        overruled = []
        for req, ans in zip(pending, answers, strict=True):
            if (ans or "").strip().lower() not in ACCEPT:
                overruled.append(req)
                self.telemetry.log("assumption_overruled", stage=req.stage, assumption=req.default_assumption)
        return overruled
