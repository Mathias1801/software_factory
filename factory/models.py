"""WHICH LLMs the factory uses, in one place.

  1. Model specs     "provider:model", e.g. "openai:gpt-5.6-luna". A bare id means Anthropic.
  2. Model sets      named cheap/mid/strong mappings in the profile (model_set: openai), plus
                     per-tier overrides (models: {strong: ...}) and per-agent overrides
                     (agents: {planner: {model: ...}})
  3. Backends        Anthropic, OpenAI, Gemini, and any OpenAI-compatible server (local models)
  4. ModelRouter     the backend the factory talks to: routes each call to the right provider,
                     and check() reports missing packages or API keys before a run starts
  5. Pricing         USD per million tokens from the profile, for cost estimates

Switching LLMs is a profile or CLI change, never a code change:
    model_set: openai                         # whole factory on OpenAI
    models: {strong: anthropic:claude-opus-5-5}   # but keep Claude for planning and escalations
    python -m factory run --models gemini --model builder=openai:gpt-5.6-luna ...
"""
from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from typing import Any

from factory.llm import LLMResponse

TIERS = ("cheap", "mid", "strong")


# =============================================================================
# 1. MODEL SPECS
# =============================================================================
PROVIDER_ALIASES = {"claude": "anthropic", "google": "gemini"}


def parse_model_spec(spec: str) -> tuple[str, str]:
    spec = str(spec).strip()
    if ":" in spec:
        provider, model = spec.split(":", 1)
        provider = provider.strip().lower()
        return PROVIDER_ALIASES.get(provider, provider), model.strip()
    return "anthropic", spec


def model_id(spec: str) -> str:
    return parse_model_spec(spec)[1]


# =============================================================================
# 2. MODEL SETS
# =============================================================================
def effective_models(profile: dict) -> dict[str, str]:
    """model_sets[model_set] with per-tier overrides from `models:` on top."""
    sets = profile.get("model_sets", {}) or {}
    name = profile.get("model_set")
    base: dict[str, str] = {}
    if name:
        if name not in sets:
            raise ValueError(f"Unknown model_set '{name}'. Known: {', '.join(sorted(sets))}")
        base = dict(sets[name])
    base.update({k: v for k, v in (profile.get("models") or {}).items() if v})
    missing = [t for t in TIERS if t not in base]
    if missing:
        raise ValueError(f"No model configured for tier(s): {', '.join(missing)}")
    return base


def agent_model_overrides(profile: dict) -> dict[str, str]:
    return {name: cfg["model"] for name, cfg in (profile.get("agents") or {}).items()
            if isinstance(cfg, dict) and cfg.get("model")}


# =============================================================================
# 3. BACKENDS (one per provider type; each gets a concrete model id per call)
# =============================================================================
class AnthropicBackend:
    """Anthropic Messages API. System prompts are marked cacheable (they repeat every call)."""

    def __init__(self, api_key_env: str = "ANTHROPIC_API_KEY", **_: Any):
        import anthropic

        self.client = anthropic.Anthropic(api_key=os.environ.get(api_key_env))

    def complete(self, *, system: str, prompt: str, model: str, max_tokens: int) -> LLMResponse:
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        return LLMResponse(text, resp.usage.input_tokens, resp.usage.output_tokens, model)


class OpenAIBackend:
    """OpenAI Chat Completions, and any OpenAI-compatible server (Ollama, vLLM, LM Studio) via base_url.
    Output tokens include hidden reasoning tokens, so the ledger sees what you are billed for."""

    def __init__(self, api_key_env: str = "OPENAI_API_KEY", base_url: str | None = None, json_mode: bool = True,
                 max_tokens_param: str = "max_completion_tokens", **_: Any):
        import openai

        self.client = openai.OpenAI(api_key=os.environ.get(api_key_env) or "not-needed", base_url=base_url)
        self.json_mode = json_mode
        self.max_tokens_param = max_tokens_param

    def complete(self, *, system: str, prompt: str, model: str, max_tokens: int) -> LLMResponse:
        kwargs: dict[str, Any] = {self.max_tokens_param: max_tokens}
        if self.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(
            model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}], **kwargs,
        )
        usage = resp.usage
        return LLMResponse(resp.choices[0].message.content or "", getattr(usage, "prompt_tokens", 0) or 0,
                           getattr(usage, "completion_tokens", 0) or 0, model)


class GeminiBackend:
    """Google Gemini via the google-genai SDK. Thinking tokens are counted as output."""

    def __init__(self, api_key_env: str = "GEMINI_API_KEY", json_mode: bool = True, **_: Any):
        from google import genai

        self.genai = genai
        self.client = genai.Client(api_key=os.environ.get(api_key_env))
        self.json_mode = json_mode

    def complete(self, *, system: str, prompt: str, model: str, max_tokens: int) -> LLMResponse:
        config = self.genai.types.GenerateContentConfig(
            system_instruction=system, max_output_tokens=max_tokens,
            response_mime_type="application/json" if self.json_mode else None,
        )
        resp = self.client.models.generate_content(model=model, contents=prompt, config=config)
        meta = resp.usage_metadata
        out = (getattr(meta, "candidates_token_count", 0) or 0) + (getattr(meta, "thoughts_token_count", 0) or 0)
        return LLMResponse(resp.text or "", getattr(meta, "prompt_token_count", 0) or 0, out, model)


PROVIDER_TYPES: dict[str, type] = {
    "anthropic": AnthropicBackend,
    "openai": OpenAIBackend,
    "openai_compatible": OpenAIBackend,
    "gemini": GeminiBackend,
}
# provider type -> (module to import, pip package name)
PACKAGES = {
    "anthropic": ("anthropic", "anthropic"),
    "openai": ("openai", "openai"),
    "openai_compatible": ("openai", "openai"),
    "gemini": ("google.genai", "google-genai"),
}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


# =============================================================================
# 4. MODEL ROUTER
# =============================================================================
class ModelRouter:
    """The backend the factory uses for real runs. Reads the current profile on every call, so a
    mode change or an override applies immediately. Provider clients are created on first use."""

    def __init__(self, profile_getter: Callable[[], dict], backends: dict[str, Any] | None = None):
        self.profile_getter = profile_getter
        self._backends: dict[str, Any] = dict(backends or {})

    def resolve(self, tier_or_spec: str) -> tuple[str, str]:
        models = effective_models(self.profile_getter())
        return parse_model_spec(models.get(tier_or_spec, tier_or_spec))

    def _provider_cfg(self, provider: str) -> dict:
        cfg = (self.profile_getter().get("providers") or {}).get(provider)
        if cfg is None and provider in PROVIDER_TYPES:
            cfg = {}
        if cfg is None:
            raise ValueError(f"Unknown provider '{provider}'. Add it under providers: in your profile.")
        return dict(cfg)

    def backend(self, provider: str):
        if provider not in self._backends:
            cfg = self._provider_cfg(provider)
            kind = cfg.pop("type", provider)
            if kind not in PROVIDER_TYPES:
                raise ValueError(f"Provider '{provider}' has unknown type '{kind}'. Known: {', '.join(PROVIDER_TYPES)}")
            self._backends[provider] = PROVIDER_TYPES[kind](**cfg)
        return self._backends[provider]

    def complete(self, *, system: str, prompt: str, tier: str, max_tokens: int, purpose: str) -> LLMResponse:
        provider, model = self.resolve(tier)
        resp = self.backend(provider).complete(system=system, prompt=prompt, model=model, max_tokens=max_tokens)
        resp.model = f"{provider}:{model}"
        return resp

    def check(self) -> list[str]:
        """Problems that would make a run fail, found before any token is spent."""
        profile = self.profile_getter()
        try:
            specs = set(effective_models(profile).values()) | set(agent_model_overrides(profile).values())
        except ValueError as exc:
            return [str(exc)]
        problems = []
        for provider in sorted({parse_model_spec(s)[0] for s in specs}):
            try:
                cfg = self._provider_cfg(provider)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            kind = cfg.get("type", provider)
            if kind not in PACKAGES:
                problems.append(f"{provider}: unknown provider type '{kind}'")
                continue
            module, pip_name = PACKAGES[kind]
            if not _module_available(module):
                problems.append(f"{provider}: install the SDK with  pip install {pip_name}")
            env = cfg.get("api_key_env") or {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                                             "gemini": "GEMINI_API_KEY"}.get(kind)
            if env and not os.environ.get(env):
                problems.append(f"{provider}: set the {env} environment variable")
        return problems


# =============================================================================
# 5. PRICING
# =============================================================================
def price_of(spec: str, pricing: dict) -> tuple[float, float] | None:
    """(input, output) USD per million tokens, looked up by full spec first, then by model id."""
    entry = pricing.get(spec) or pricing.get(model_id(spec)) if pricing else None
    if not entry:
        return None
    return float(entry[0]), float(entry[1])


def cost_usd(input_tokens: int, output_tokens: int, spec: str, pricing: dict) -> float | None:
    price = price_of(spec, pricing)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1e6


def cost_by_stage(llm_calls: list[dict], pricing: dict) -> tuple[dict[str, float], set[str]]:
    """USD per stage from telemetry llm_call events, plus the models that had no price."""
    totals: dict[str, float] = {}
    unpriced: set[str] = set()
    for call in llm_calls:
        cost = cost_usd(int(call.get("input_tokens", 0)), int(call.get("output_tokens", 0)), str(call.get("model", "")), pricing)
        if cost is None:
            unpriced.add(str(call.get("model", "?")))
            continue
        totals[call.get("stage", "?")] = totals.get(call.get("stage", "?"), 0.0) + cost
    return totals, unpriced
