"""Scripted backend for dry runs and tests. Scripts are keyed by prompt name (see tasks.PROMPTS). Produces a tiny calculator project
so the whole pipeline can run end to end without an API key or token spend."""
from __future__ import annotations

import json
from collections import defaultdict

from factory.llm import LLMResponse

_CORE = '''"""Basic arithmetic helpers."""


def add(a: float, b: float) -> float:
    """Return the sum of a and b."""
    return a + b


def subtract(a: float, b: float) -> float:
    """Return a minus b."""
    return a - b
'''

_INIT = '"""Tiny calculator package."""\nfrom .core import add, subtract\n\n__all__ = ["add", "subtract"]\n'

_TEST = '''from calc import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2
'''


_ACCEPT_AC1 = '''from calc import add


def test_add_returns_sum():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
'''

_ACCEPT_AC2 = '''from calc import subtract


def test_subtract_returns_difference():
    assert subtract(5, 3) == 2
'''


def default_mock_scripts() -> dict[str, list[str]]:
    classify = {"mode": "prototype", "reason": "Small demo package with two functions."}
    critique = {"issues": [{
        "type": "missing_edge_case",
        "detail": "Behaviour for non-numeric input is not specified.",
        "question": "Should add() and subtract() reject non-numeric input?",
    }]}
    acceptance = {
        "edits": [
            {"op": "write", "path": "tests/acceptance/test_ac1.py", "content": _ACCEPT_AC1},
            {"op": "write", "path": "tests/acceptance/test_ac2.py", "content": _ACCEPT_AC2},
        ],
        "interface_notes": "Package `calc` at the app root exposing add(a, b) and subtract(a, b).",
    }
    handoff = {"readme": "# calc\n\nTiny calculator package.\n\n## Test\n\n```bash\npytest\n```"}
    interview_round_1 = {
        "done": False,
        "questions": [
            {"q": "Who will use this and for what?", "theme": "users_goals", "blocking": True},
            {"q": "Any preferred packaging or Python version?", "theme": "constraints", "blocking": False},
        ],
    }
    interview_done = {"done": True, "questions": []}
    readback = {
        "readback": "You want a small Python calculator package with add and subtract, covered by tests.",
        "in_scope": ["add", "subtract", "unit tests"],
        "out_of_scope": ["GUI", "CLI", "persistence"],
    }
    spec = {
        "summary": "A tiny Python calculator package.",
        "in_scope": ["add and subtract functions", "unit tests"],
        "out_of_scope": ["GUI", "CLI"],
        "assumptions": ["Python 3.10+ (assumed, not stated)"],
        "open_questions": [],
        "acceptance_criteria": [
            {"id": "AC1", "text": "add(a, b) returns the sum"},
            {"id": "AC2", "text": "subtract(a, b) returns a minus b"},
        ],
        "tech_stack": {"language": "python", "tests": "pytest"},
        "tasks": [
            {
                "id": "T1",
                "title": "Implement calculator core",
                "description": "Create calc package with add and subtract plus tests.",
                "acceptance_ids": ["AC1", "AC2"],
                "domain_tags": [],
                "risk_tags": [],
                "files_hint": [],
            }
        ],
    }
    build = {
        "edits": [
            {"op": "write", "path": "conftest.py", "content": '"""Makes the repo root importable in tests."""\n'},
            {"op": "write", "path": "calc/__init__.py", "content": _INIT},
            {"op": "write", "path": "calc/core.py", "content": _CORE},
            {"op": "write", "path": "tests/test_core.py", "content": _TEST},
        ],
        "decisions": [
            {
                "title": "Use plain functions instead of a Calculator class",
                "options": ["functions", "class"],
                "chosen": "functions",
                "rationale": "Simplest thing that meets the criteria.",
                "reversibility": "cheap",
                "category": "implementation",
                "touches_spec_scope": False,
                "risk_tags": [],
            }
        ],
        "questions": [
            {
                "summary": "Should division be added later?",
                "blocking": False,
                "default_assumption": "No, division is out of scope for now.",
            }
        ],
        "notes": "Created calc package and tests.",
    }
    build_tool_round = {
        "tool_calls": [{"tool": "statistics", "args": {"values": [2, 3, 5, 7]}}],
        "tool_suggestions": [{
            "tool": "web_search",
            "reason": "Check current Python packaging conventions",
            "expected_benefit": "Avoid outdated project layout",
        }],
    }
    review = {"pass": True, "issues": []}
    return {
        "classify_mode": [json.dumps(classify)],
        "critique_spec": [json.dumps(critique)],
        "acceptance_tests": [json.dumps(acceptance)],
        "handoff": [json.dumps(handoff)],
        "interview": [json.dumps(interview_round_1), json.dumps(interview_done)],
        "readback": [json.dumps(readback)],
        "spec": [json.dumps(spec)],
        "build": [json.dumps(build_tool_round), json.dumps(build)],
        "review": [json.dumps(review)],
    }


class MockBackend:
    def __init__(self, scripts: dict[str, list[str]] | None = None):
        self.scripts = scripts or default_mock_scripts()
        self.counters: defaultdict[str, int] = defaultdict(int)

    def complete(self, *, system: str, prompt: str, tier: str, max_tokens: int, purpose: str) -> LLMResponse:
        options = self.scripts.get(purpose) or ["{}"]
        i = self.counters[purpose]
        self.counters[purpose] += 1
        text = options[min(i, len(options) - 1)]
        return LLMResponse(text, (len(system) + len(prompt)) // 4, len(text) // 4, f"mock-{tier}")
