import json

import pytest

from factory.agents.tools import TOOLS, MockWebSearch, ToolBroker, expenses_tool, statistics_tool
from factory.artifacts import ArtifactStore
from factory.budget import TokenLedger
from factory.config import deep_merge, load_profile
from factory.human import HumanLoop, ScriptedHumanChannel
from factory.llm import MeteredLLM
from factory.mock import MockBackend
from factory.telemetry import Telemetry

PROFILE = load_profile()
ALL = ("statistics", "expenses", "web_search")


def make_broker(tmp_path, answers=None, profile=PROFILE, providers=None):
    store = ArtifactStore(tmp_path / "p")
    telemetry = Telemetry(store.path("telemetry.jsonl"))
    ledger = TokenLedger(profile["budget"]["stage_tokens"])
    llm = MeteredLLM(MockBackend(), ledger, telemetry)
    human = HumanLoop(ScriptedHumanChannel(answers), telemetry)
    return ToolBroker(profile, store, human, telemetry, llm, providers), store, ledger


def test_statistics_tool():
    out = json.loads(statistics_tool({"values": [1, 2, 3, 4]}, {}).output)
    assert out["mean"] == 2.5 and out["median"] == 2.5 and out["count"] == 4


def test_expenses_tool():
    out = json.loads(expenses_tool({"items": [{"category": "a", "amount": 10, "quantity": 2},
                                              {"category": "b", "amount": 5}], "budget": 20}, {}).output)
    assert out["total"] == 25 and out["remaining"] == -5 and out["over_budget"] is True


def test_bad_args_raise():
    with pytest.raises(ValueError):
        statistics_tool({"values": []}, {})


def test_enabled_tool_runs_and_disabled_is_refused(tmp_path):
    broker, _, _ = make_broker(tmp_path)
    out = broker.run_calls([{"tool": "statistics", "args": {"values": [1, 3]}}, {"tool": "web_search", "args": {}}],
                           stage="build", agent="builder", allowed=ALL)
    assert '"mean": 2.0' in out and "not available" in out


def test_agent_allow_list_is_respected(tmp_path):
    broker, _, _ = make_broker(tmp_path)
    out = broker.run_calls([{"tool": "statistics", "args": {"values": [1]}}], stage="qa", agent="reviewer", allowed=())
    assert "not available" in out


def test_suggestion_enable_persists(tmp_path):
    broker, store, _ = make_broker(tmp_path, answers=["enable for this project"])
    broker.collect_suggestions([{"tool": "web_search", "reason": "docs"}], stage="build", agent="builder")
    broker.review_suggestions()
    assert "web_search" in broker.enabled
    assert store.read_json("tool_policy.json")["enabled"] == ["web_search"]
    assert "web_search" in store.read_text("tool_suggestions.md")


def test_never_denies_and_stops_suggestions(tmp_path):
    broker, _, _ = make_broker(tmp_path, answers=["never suggest again"])
    broker.collect_suggestions([{"tool": "web_search"}], stage="build", agent="builder")
    broker.review_suggestions()
    assert "web_search" in broker.denied
    broker.collect_suggestions([{"tool": "web_search"}], stage="build", agent="builder")
    assert not broker.pending


def test_profile_denied_overrides_enabled(tmp_path):
    broker, _, _ = make_broker(tmp_path, profile=deep_merge(PROFILE, {"tools": {"denied": ["statistics"]}}))
    assert "statistics" not in broker.enabled


def test_suggestions_off(tmp_path):
    broker, _, _ = make_broker(tmp_path, profile=deep_merge(PROFILE, {"tools": {"suggestions": "off"}}))
    broker.collect_suggestions([{"tool": "web_search"}], stage="build", agent="builder")
    assert not broker.pending


def test_token_costing_tool_is_recorded(tmp_path):
    profile = deep_merge(PROFILE, {"tools": {"enabled": ["web_search"]}})
    broker, _, ledger = make_broker(tmp_path, profile=profile, providers={"web_search": MockWebSearch()})
    out = broker.run_calls([{"tool": "web_search", "args": {"query": "fastapi"}}], stage="build", agent="b", allowed=ALL)
    assert "(mock)" in out and ledger.used["tools"] == 400


def test_registry_has_cost_labels():
    assert TOOLS["statistics"].cost_label.startswith("free")
    assert "tokens" in TOOLS["web_search"].cost_label
