from types import SimpleNamespace

import pytest

from factory.config import deep_merge, load_profile
from factory.llm import LLMResponse
from factory.models import (
    GeminiBackend,
    ModelRouter,
    OpenAIBackend,
    cost_by_stage,
    effective_models,
    parse_model_spec,
)

PROFILE = load_profile()


class FakeBackend:
    def __init__(self, name):
        self.name, self.calls = name, []

    def complete(self, *, system, prompt, model, max_tokens):
        self.calls.append(model)
        return LLMResponse("{}", 10, 5, model)


def test_parse_model_spec():
    assert parse_model_spec("claude-sonnet-5") == ("anthropic", "claude-sonnet-5")
    assert parse_model_spec("openai:gpt-5.6-luna") == ("openai", "gpt-5.6-luna")
    assert parse_model_spec("google:gemini-3.1-pro") == ("gemini", "gemini-3.1-pro")
    assert parse_model_spec("local:qwen3:8b") == ("local", "qwen3:8b")  # model ids may contain colons


def test_every_model_set_is_complete():
    for name in PROFILE["model_sets"]:
        assert set(effective_models({**PROFILE, "model_set": name})) >= {"cheap", "mid", "strong"}


def test_tier_override_on_top_of_set():
    profile = deep_merge(PROFILE, {"model_set": "openai", "models": {"strong": "anthropic:claude-opus-5-5"}})
    models = effective_models(profile)
    assert models["cheap"] == "openai:gpt-5.6-luna" and models["strong"] == "anthropic:claude-opus-5-5"


def test_unknown_set_is_reported():
    with pytest.raises(ValueError):
        effective_models({**PROFILE, "model_set": "nope"})


def test_router_sends_each_tier_to_its_provider():
    profile = deep_merge(PROFILE, {"model_set": "mixed_budget"})
    fakes = {p: FakeBackend(p) for p in ("openai", "gemini", "anthropic")}
    router = ModelRouter(lambda: profile, backends=fakes)
    assert router.complete(system="s", prompt="p", tier="cheap", max_tokens=10, purpose="x").model == "openai:gpt-5.6-luna"
    router.complete(system="s", prompt="p", tier="mid", max_tokens=10, purpose="x")
    router.complete(system="s", prompt="p", tier="anthropic:claude-opus-5-5", max_tokens=10, purpose="x")  # agent pin
    assert fakes["gemini"].calls == ["gemini-3.8-flash"]
    assert fakes["anthropic"].calls == ["claude-opus-5-5"]


def test_check_reports_missing_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    problems = ModelRouter(lambda: deep_merge(PROFILE, {"model_set": "openai"})).check()
    assert any("OPENAI_API_KEY" in p for p in problems)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert not [p for p in ModelRouter(lambda: deep_merge(PROFILE, {"model_set": "openai"})).check() if "KEY" in p]


def test_check_reports_unknown_provider():
    problems = ModelRouter(lambda: deep_merge(PROFILE, {"models": {"cheap": "mystery:model"}})).check()
    assert any("Unknown provider 'mystery'" in p for p in problems)


def test_openai_backend_request_shape(monkeypatch):
    backend = OpenAIBackend(api_key_env="NOPE")
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"a": 1}'))],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=40))

    monkeypatch.setattr(backend.client.chat.completions, "create", create)
    resp = backend.complete(system="sys", prompt="p", model="gpt-5.6-luna", max_tokens=500)
    assert resp.text == '{"a": 1}' and resp.input_tokens == 100 and resp.output_tokens == 40
    assert seen["max_completion_tokens"] == 500 and seen["response_format"] == {"type": "json_object"}
    assert seen["messages"][0] == {"role": "system", "content": "sys"}


def test_gemini_backend_counts_thinking_tokens(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    backend = GeminiBackend()
    meta = SimpleNamespace(prompt_token_count=100, candidates_token_count=30, thoughts_token_count=20)
    monkeypatch.setattr(backend.client.models, "generate_content",
                        lambda **kw: SimpleNamespace(text='{"ok": true}', usage_metadata=meta))
    resp = backend.complete(system="s", prompt="p", model="gemini-3.8-flash", max_tokens=100)
    assert resp.input_tokens == 100 and resp.output_tokens == 50


def test_cost_by_stage():
    calls = [{"stage": "build", "model": "anthropic:claude-haiku-4-5-20251001", "input_tokens": 1_000_000, "output_tokens": 0},
             {"stage": "plan", "model": "mystery:x", "input_tokens": 10, "output_tokens": 10}]
    costs, unpriced = cost_by_stage(calls, PROFILE["pricing"])
    assert costs == {"build": 1.0} and unpriced == {"mystery:x"}
