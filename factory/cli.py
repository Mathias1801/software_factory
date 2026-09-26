"""Command line entry point: python -m factory run | models | dashboard"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from factory.agents import AGENTS, Factory
from factory.agents.tools import MockWebSearch
from factory.config import DEFAULT_PROFILE, ProfileLayers, deep_merge, load_layers, resolve
from factory.human import CLIHumanChannel, ScriptedHumanChannel
from factory.mock import MockBackend
from factory.models import TIERS, ModelRouter, agent_model_overrides, effective_models, price_of

MODES = ["auto", "simple", "prototype", "fullstack"]


def project_name_from(idea: str) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", idea.lower())[:5]
    return f"{date.today():%Y-%m-%d}_{'_'.join(words) or 'project'}"


def apply_model_args(layers: ProfileLayers, model_set: str | None, pins: list[str]) -> None:
    """--models <set> and --model <tier|agent>=<provider:model> become user-profile overrides."""
    override: dict = {}
    if model_set:
        override["model_set"] = model_set
    for pin in pins or []:
        if "=" not in pin:
            raise SystemExit(f"--model expects TIER=MODEL or AGENT=MODEL, got '{pin}'")
        key, spec = (x.strip() for x in pin.split("=", 1))
        if key in TIERS:
            override.setdefault("models", {})[key] = spec
        elif key in AGENTS:
            override.setdefault("agents", {}).setdefault(key, {})["model"] = spec
        else:
            raise SystemExit(f"'{key}' is not a tier ({', '.join(TIERS)}) or an agent ({', '.join(AGENTS)})")
    layers.user = deep_merge(layers.user, override)


def print_models(profile: dict) -> int:
    pricing = profile.get("pricing", {})

    def label(spec: str) -> str:
        price = price_of(spec, pricing)
        return f"{spec}" + (f"   (${price[0]:g} / ${price[1]:g} per M tokens)" if price else "   (no price set)")

    print(f"Model set: {profile.get('model_set')}")
    try:
        for tier, spec in effective_models(profile).items():
            print(f"  {tier:<7} {label(spec)}")
    except ValueError as exc:
        print(f"  ERROR: {exc}")
    pins = agent_model_overrides(profile)
    if pins:
        print("Agent pins:")
        for agent, spec in pins.items():
            print(f"  {agent:<16} {label(spec)}")
    print("\nAvailable sets: " + ", ".join(profile.get("model_sets", {})))
    problems = ModelRouter(lambda: profile).check()
    print("\nReady to run." if not problems else "\nNot ready:\n" + "\n".join(f"  - {p}" for p in problems))
    return 0 if not problems else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory", description="Agentic software factory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def model_flags(p):
        p.add_argument("--profile", default=str(DEFAULT_PROFILE), help="Project profile YAML")
        p.add_argument("--models", help="Model set to use (see: python -m factory models)")
        p.add_argument("--model", action="append", default=[], metavar="TIER_OR_AGENT=MODEL",
                       help="Override one tier or agent, e.g. strong=anthropic:claude-opus-5-5 or builder=openai:gpt-5.6-luna")

    run = sub.add_parser("run", help="Plan, build, test and deliver a project into its own master folder")
    run.add_argument("--project", help="Master folder name. Defaults to <date>_<first words of the idea>.")
    run.add_argument("--idea", help="Short description of what to build")
    run.add_argument("--idea-file", help="Text or markdown file describing what to build")
    run.add_argument("--mode", choices=MODES, help="Build mode; default comes from the profile (auto)")
    run.add_argument("--workspace", help="Folder that holds project master folders (default: profile workspace)")
    run.add_argument("--mock", action="store_true", help="Use the scripted mock model (no API calls)")
    run.add_argument("--auto-approve", action="store_true", help="Answer human requests automatically (dry runs only)")
    run.add_argument("--fresh", action="store_true", help="Delete the existing master folder first")
    model_flags(run)

    models = sub.add_parser("models", help="Show which LLMs will be used and whether keys and packages are ready")
    model_flags(models)

    dash = sub.add_parser("dashboard", help="Build the dashboard for a project on demand")
    dash.add_argument("--project", required=True)
    dash.add_argument("--profile", default=str(DEFAULT_PROFILE))
    dash.add_argument("--workspace")

    args = parser.parse_args(argv)
    layers = load_layers(args.profile)
    if args.cmd in ("run", "models"):
        apply_model_args(layers, args.models, args.model)
    if args.cmd == "models":
        return print_models(resolve(layers))

    workspace = Path(args.workspace or layers.user.get("workspace") or layers.base.get("workspace", "factory_projects"))
    workspace = workspace.expanduser()

    if args.cmd == "dashboard":
        print(Factory(layers, workspace / args.project, MockBackend(), ScriptedHumanChannel()).build_dashboard())
        return 0

    idea = Path(args.idea_file).read_text(encoding="utf-8") if args.idea_file else args.idea
    if not args.project and not idea:
        parser.error("give --idea/--idea-file for a new project, or --project to resume one")

    backend, providers = None, {}
    if args.mock:
        backend, providers = MockBackend(), {"web_search": MockWebSearch()}
    else:
        profile = resolve(layers)
        problems = ModelRouter(lambda: profile).check()
        if problems:  # fail before creating folders or spending tokens
            print("Cannot start, the configured models are not ready:\n" + "\n".join(f"  - {p}" for p in problems),
                  file=sys.stderr)
            return 2
        if os.environ.get("ANTHROPIC_API_KEY"):
            from factory.agents.tools import AnthropicWebSearch

            providers = {"web_search": AnthropicWebSearch(profile["tools"].get("web_search_model", "claude-haiku-4-5-20251001"))}

    project_dir = workspace / (args.project or project_name_from(idea or ""))
    if args.fresh and project_dir.exists():
        shutil.rmtree(project_dir)
    print(f"Master folder: {project_dir}")
    channel = ScriptedHumanChannel() if args.auto_approve else CLIHumanChannel()
    summary = Factory(layers, project_dir, backend, channel, providers, mode=args.mode).run(idea)
    print(json.dumps(summary, indent=2))
    return 0 if summary["outcome"] == "completed" else 1
