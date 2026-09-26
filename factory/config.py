"""Profile loading with three layers, applied in this order:

    profiles/default.yaml  <  modes.<mode>.overrides  <  your project profile

So a mode (simple, prototype, fullstack) adapts the recipe, and anything you set explicitly in
your own profile always wins over the mode."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = ROOT / "profiles"
DEFAULT_PROFILE = PROFILES_DIR / "default.yaml"


def deep_merge(base: dict, override: dict | None) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass
class ProfileLayers:
    base: dict
    user: dict

    def mode(self, cli_mode: str | None = None) -> str:
        return cli_mode or self.user.get("mode") or self.base.get("mode", "auto")

    def modes(self) -> dict:
        return deep_merge(self.base.get("modes", {}), self.user.get("modes", {}))


def load_layers(path: str | Path | None = None) -> ProfileLayers:
    base = yaml.safe_load(DEFAULT_PROFILE.read_text(encoding="utf-8")) or {}
    user: dict = {}
    if path is not None and Path(path).resolve() != DEFAULT_PROFILE.resolve():
        user = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return ProfileLayers(base, user)


def resolve(layers: ProfileLayers, mode: str | None = None) -> dict[str, Any]:
    profile = copy.deepcopy(layers.base)
    if mode and mode != "auto":
        modes = layers.modes()
        if mode not in modes:
            raise ValueError(f"Unknown mode '{mode}'. Known: {', '.join(sorted(modes))}")
        profile = deep_merge(profile, modes[mode].get("overrides", {}))
    profile = deep_merge(profile, layers.user)
    profile["mode"] = mode or profile.get("mode", "auto")
    return profile


def load_profile(path: str | Path | None = None, mode: str | None = None) -> dict[str, Any]:
    return resolve(load_layers(path), mode)


def describe_modes(modes: dict) -> str:
    return "\n".join(f"- {name}: {m.get('description', '')}" for name, m in modes.items())
