import json

from factory.agents import Factory
from factory.config import PROFILES_DIR, deep_merge, load_layers
from factory.human import ScriptedHumanChannel
from factory.mock import MockBackend


def events(project):
    return [json.loads(line) for line in (project / "factory_state" / "telemetry.jsonl").read_text().splitlines()]


def test_fullstack_run_uses_every_quality_step(tmp_path):
    layers = load_layers(PROFILES_DIR / "dry_run.yaml")
    layers.user = deep_merge(layers.user, {"stages": {"qa": {"llm_review": "always"}}})
    channel = ScriptedHumanChannel()
    summary = Factory(layers, tmp_path / "calc", MockBackend(), channel, mode="fullstack").run("A tiny calculator")

    assert summary["outcome"] == "completed", summary
    assert summary["completed_tasks"] == ["T1"] and summary["integration_passed"] is True
    project = tmp_path / "calc"
    for rel in ["PROJECT.md", "dashboard.html", "app/calc/core.py", "app/README.md", "app/tests/acceptance/test_ac1.py",
                "docs/idea.md", "docs/spec.md", "docs/interview.md", "docs/acceptance_tests.md",
                "factory_state/spec.json", "factory_state/profile_resolved.yaml", "factory_state/deploy_manifest.json"]:
        assert (project / rel).exists(), rel
    assert not list(tmp_path.glob("*.json")), "nothing may be written outside the master folder"

    kinds = {r.kind for r in channel.asked}
    assert {"question", "readback", "approval", "spec_issue", "tool_suggestion"} <= kinds
    ev = events(project)
    names = {e["event"] for e in ev}
    assert {"spec_critique", "acceptance_tests", "integration_check", "handoff", "tool_call"} <= names
    assert [e for e in ev if e["event"] == "task_result"][0]["first_pass"] is True
    assert any(e["event"] == "llm_call" and e["purpose"] == "review" for e in ev)


def test_agent_model_pin_reaches_the_backend(tmp_path):
    layers = load_layers(PROFILES_DIR / "dry_run.yaml")
    layers.user = deep_merge(layers.user, {"agents": {"planner": {"model": "openai:gpt-5.6-sol"}}})
    Factory(layers, tmp_path / "p", MockBackend(), ScriptedHumanChannel(), mode="simple").run("calc")
    planner_tiers = {e["tier"] for e in events(tmp_path / "p") if e["event"] == "llm_call" and e["purpose"] == "spec"}
    assert planner_tiers == {"openai:gpt-5.6-sol"}
    assert "Estimated cost" in (tmp_path / "p" / "PROJECT.md").read_text()


def test_auto_mode_picks_prototype_and_skips_heavy_steps(tmp_path):
    summary = Factory(load_layers(PROFILES_DIR / "dry_run.yaml"), tmp_path / "p", MockBackend(),
                      ScriptedHumanChannel()).run("A tiny calculator")
    assert summary["outcome"] == "completed" and summary["mode"] == "prototype"
    assert summary["dashboard"] is None and not (tmp_path / "p" / "dashboard.html").exists()
    assert not (tmp_path / "p" / "app" / "tests" / "acceptance").exists()
    assert (tmp_path / "p" / "PROJECT.md").exists()
    assert any(e["event"] == "review_skipped" for e in events(tmp_path / "p"))


def test_resume_spends_no_tokens(tmp_path):
    layers = load_layers(PROFILES_DIR / "dry_run.yaml")
    Factory(layers, tmp_path / "p", MockBackend(), ScriptedHumanChannel(), mode="fullstack").run("calc")
    backend = MockBackend()
    summary = Factory(layers, tmp_path / "p", backend, ScriptedHumanChannel()).run(None)
    assert summary["outcome"] == "completed" and summary["mode"] == "fullstack"
    assert sum(backend.counters.values()) == 0


def test_answered_critic_issue_triggers_one_redraft(tmp_path):
    answers = ["users", "no preference", "y", "Yes, raise TypeError for non-numbers"]
    backend = MockBackend()
    Factory(load_layers(PROFILES_DIR / "dry_run.yaml"), tmp_path / "p", backend, ScriptedHumanChannel(answers),
            mode="prototype").run("calc")
    assert backend.counters["spec"] == 2
