"""Everything about agents, in four files:

- agents.py  ALL agents (role + tasks + tools) and the logistics between them: routing and the pipeline
- roles.py   ALL roles: who an agent is (system prompts, core roles, specialists)
- tasks.py   ALL tasks: each task's prompt(s) next to its procedure
- tools.py   ALL tools: model tools, file/context/check/shell tools, and the ToolBroker
"""
from factory.agents.agents import AGENTS, Agent, AgentSpec, Factory, pick_builder, pick_tier
from factory.agents.roles import ROLES, get_role, specialist_names
from factory.agents.tasks import PROMPTS, TASKS
from factory.agents.tools import TOOLS, ToolBroker

__all__ = [
    "AGENTS", "PROMPTS", "ROLES", "TASKS", "TOOLS", "Agent", "AgentSpec", "Factory", "ToolBroker",
    "get_role", "pick_builder", "pick_tier", "specialist_names",
]
