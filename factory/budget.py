"""Token accounting per stage. Every model call goes through the ledger."""
from __future__ import annotations

from collections import defaultdict


class BudgetExceeded(Exception):
    def __init__(self, stage: str, projected: int, budget: int):
        super().__init__(f"{stage}: projected {projected} tokens exceeds budget {budget}")
        self.stage = stage
        self.projected = projected
        self.budget = budget


class TokenLedger:
    def __init__(self, stage_budgets: dict[str, int], warn_at: float = 0.8):
        self.budgets: dict[str, int] = dict(stage_budgets)
        self.warn_at = warn_at
        self.used: defaultdict[str, int] = defaultdict(int)

    def record(self, stage: str, input_tokens: int, output_tokens: int) -> None:
        self.used[stage] += int(input_tokens) + int(output_tokens)

    def remaining(self, stage: str) -> int | None:
        budget = self.budgets.get(stage)
        if budget is None:
            return None
        return max(0, budget - self.used[stage])

    def check(self, stage: str, estimated_next: int = 0) -> str:
        budget = self.budgets.get(stage)
        if budget is None:
            return "ok"
        projected = self.used[stage] + estimated_next
        if projected > budget:
            raise BudgetExceeded(stage, projected, budget)
        if projected > budget * self.warn_at:
            return "warn"
        return "ok"

    def extend(self, stage: str, factor: float, at_least: int = 0) -> int:
        current = self.budgets.get(stage, 0)
        self.budgets[stage] = max(int(current * factor), at_least)
        return self.budgets[stage]

    def total_used(self) -> int:
        return sum(self.used.values())

    def report(self) -> dict[str, dict[str, int]]:
        stages = set(self.used) | set(self.budgets)
        return {s: {"used": self.used[s], "budget": self.budgets.get(s, 0)} for s in sorted(stages)}

    def to_dict(self) -> dict:
        return {"budgets": self.budgets, "used": dict(self.used)}

    def load(self, data: dict) -> None:
        for stage, budget in (data.get("budgets") or {}).items():
            self.budgets[stage] = max(self.budgets.get(stage, 0), int(budget))
        for stage, used in (data.get("used") or {}).items():
            self.used[stage] = int(used)
