"""Metered model access. MeteredLLM wraps any backend with budget checks and telemetry, so no agent
can spend tokens without the ledger knowing. WHICH model and provider handle a call is decided in
factory/models.py (model sets, providers, backends, pricing)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol

from factory.budget import BudgetExceeded, TokenLedger
from factory.telemetry import Telemetry


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


class LLMBackend(Protocol):
    """'tier' is a tier name (cheap | mid | strong) or a model spec such as 'openai:gpt-5.6-luna'."""

    def complete(self, *, system: str, prompt: str, tier: str, max_tokens: int, purpose: str) -> LLMResponse: ...


def extract_json(text: str) -> dict:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("No JSON object found in model output") from None
        data = json.loads(s[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object from the model")
    return data


class MeteredLLM:
    def __init__(
        self,
        backend: LLMBackend,
        ledger: TokenLedger,
        telemetry: Telemetry,
        on_budget_exceeded: Callable[[BudgetExceeded], bool] | None = None,
        extend_factor: float = 1.5,
        default_max_tokens: int = 4000,
    ):
        self.backend = backend
        self.ledger = ledger
        self.telemetry = telemetry
        self.on_budget_exceeded = on_budget_exceeded
        self.extend_factor = extend_factor
        self.default_max_tokens = default_max_tokens

    def _estimate(self, system: str, prompt: str, max_tokens: int) -> int:
        # Rough: ~4 chars per token for input, assume a quarter of max output is used.
        return (len(system) + len(prompt)) // 4 + max_tokens // 4

    def reserve(self, stage: str, estimate: int) -> None:
        """Check the budget before spending. Asks via on_budget_exceeded, extends or raises."""
        while True:
            try:
                status = self.ledger.check(stage, estimate)
                break
            except BudgetExceeded as err:
                self.telemetry.log("budget_exceeded", stage=stage, projected=err.projected, budget=err.budget)
                if self.on_budget_exceeded is None or not self.on_budget_exceeded(err):
                    raise
                new_budget = self.ledger.extend(stage, self.extend_factor, at_least=err.projected + 1)
                self.telemetry.log("budget_extended", stage=stage, new_budget=new_budget)
        if status == "warn":
            self.telemetry.log("budget_warning", stage=stage, used=self.ledger.used[stage])

    def record_external(
        self, stage: str, input_tokens: int, output_tokens: int, model: str, agent: str, purpose: str
    ) -> None:
        """Record tokens spent outside call(), e.g. by a tool that uses a model."""
        self.ledger.record(stage, input_tokens, output_tokens)
        self.telemetry.log(
            "llm_call", stage=stage, purpose=purpose, tier="tool", agent=agent, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )

    def call(
        self,
        *,
        stage: str,
        purpose: str,
        system: str,
        prompt: str,
        tier: str = "cheap",
        agent: str = "generalist",
        max_tokens: int | None = None,
    ) -> LLMResponse:
        max_tokens = max_tokens or self.default_max_tokens
        self.reserve(stage, self._estimate(system, prompt, max_tokens))
        resp = self.backend.complete(system=system, prompt=prompt, tier=tier, max_tokens=max_tokens, purpose=purpose)
        self.ledger.record(stage, resp.input_tokens, resp.output_tokens)
        self.telemetry.log(
            "llm_call",
            stage=stage,
            purpose=purpose,
            tier=tier,
            agent=agent,
            model=resp.model,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
        )
        return resp

    def call_json(self, **kwargs) -> dict:
        resp = self.call(**kwargs)
        try:
            return extract_json(resp.text)
        except (ValueError, json.JSONDecodeError):
            kwargs["prompt"] = (
                kwargs["prompt"] + "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object."
            )
            return extract_json(self.call(**kwargs).text)
