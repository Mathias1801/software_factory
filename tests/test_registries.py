from string import Formatter

from factory.agents import AGENTS, PROMPTS, ROLES, TASKS, TOOLS, specialist_names
from factory.config import load_layers, load_profile


def test_agents_reference_existing_roles_tasks_and_tools():
    for agent in AGENTS.values():
        assert agent.role in ROLES
        assert set(agent.tasks) <= set(TASKS), agent.name
        assert set(agent.tools) <= set(TOOLS), agent.name


def test_every_task_is_owned_by_exactly_one_agent():
    owners = [t for a in AGENTS.values() for t in a.tasks]
    assert sorted(owners) == sorted(TASKS)


def test_tasks_reference_existing_prompts():
    for task in TASKS.values():
        assert set(task.prompts) <= set(PROMPTS)


def test_prompts_render():
    for prompt in PROMPTS.values():
        fields = {f for _, f, _, _ in Formatter().parse(prompt.instructions) if f}
        assert "Return a single JSON object" in prompt.render("TOOLS", **{f: "x" for f in fields})


def test_profile_agents_and_specialists_exist():
    profile = load_profile()
    assert set(profile["agents"]) <= set(AGENTS)
    assert set(profile["specialists"]["allowed"]) <= set(specialist_names())


def test_every_mode_resolves():
    for mode in load_layers().modes():
        profile = load_profile(mode=mode)
        assert profile["mode"] == mode
        assert set(profile["guidance"]) <= set(AGENTS)
