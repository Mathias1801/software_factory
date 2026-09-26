# CLAUDE.md

Guidance for Claude Code working in this repository. Read this first, then README.md.

## What this project is

An agentic software factory: one reusable pipeline that turns an idea into delivered software in
its own master folder, adapting to the project via build modes (simple, prototype, fullstack).
Principles that every change must respect:

1. **Cost discipline:** cheap generalist by default; specialists, strong models and heavy quality steps only when rules or the mode say they pay off. Every token goes through a budgeted ledger.
2. **Honesty with the human:** ask instead of inventing requirements; costly or scope-changing decisions need approval; planning ends with an explicit, signed-off spec.
3. **Adaptability:** stages, checks, quality steps, specialists, tools, autonomy, budgets and outputs are profile or mode settings, not code.
4. **Tools over guesswork:** arithmetic, statistics, costs and current facts come from tools. Agents may suggest tools; the human decides and can override.
5. **One master folder per project:** nothing a run creates may be written outside it.
6. **LLMs are configuration:** switching providers or models is a profile or CLI change (`model_set`, `models`, `agents.<name>.model`, `--models`, `--model`), never a code change.

## Commands

```bash
pip install -r requirements.txt
python -m pytest -q                               # all tests, no API key needed
python -m ruff check factory tests                # lint (line length 130)
python -m factory run --idea "tiny calculator" --profile profiles/dry_run.yaml --mock --auto-approve
python -m factory run --idea-file idea.md --mode fullstack        # real run (ANTHROPIC_API_KEY)
python -m factory run --project <folder name>                     # resume
python -m factory models --models openai                          # show models in use, check keys/packages
python -m factory run --models gemini --model strong=anthropic:claude-opus-5-5 --idea-file idea.md
python -m factory dashboard --project <folder name>               # dashboard on demand
```

Run the full test suite after every change. `tests/test_registries.py` catches broken wiring
between agents, roles, tasks, prompts, tools and modes; `tests/test_end_to_end.py` runs the whole
pipeline with the mock model.

## Structure: four agent files, everything else references them

```
factory/agents/
  agents.py    1 AgentSpec + AGENTS   agent = role + tasks + allowed model tools (declarative)
               2 Agent                runtime: ask() (prompt + role + guidance + tool rounds), do(), colleague()
               3 Routing              pick_builder, pick_tier, FailureTracker
               4 Factory              pipeline + logistics: modes, stages, task loop, estimate, PROJECT.md
  roles.py     1 core roles (planner, critic, test_engineer, builder, reviewer, release_manager)
               2 specialist roles (security, database, frontend)   3 helpers
  tasks.py     0 types (Prompt, TaskDef, results)  1..12 one section per task, prompt next to procedure
               13 registries PROMPTS and TASKS
  tools.py     1 model tools (statistics, expenses, web_search)  2 file tools  3 context tools
               4 check tools (compile, pytest, ruff, mypy, bandit)  5 shell and HTTP  6 ToolBroker
factory/       infrastructure referenced by the agent files:
  config.py      profile layers: default.yaml < modes.<mode>.overrides < user profile; resolve()
  artifacts.py   master folder layout and file routing (*.md -> docs/, PROJECT.md/*.html -> root, else factory_state/)
  models.py      WHICH LLMs: model specs (provider:id), model sets, per-tier/agent overrides, backends
                 (Anthropic, OpenAI, Gemini, OpenAI-compatible), ModelRouter (+ check()), pricing
  llm.py         MeteredLLM (reserve, record_external, call, call_json), LLMBackend protocol
  budget.py      TokenLedger       human.py   HumanRequest, channels, HumanLoop
  decisions.py   approval policy   knowledge.py  cross-project lessons and cost averages (no LLM)
  state.py       Spec, Task, RunState    telemetry.py  JSONL events    dashboard.py  optional HTML
  mock.py        scripted replies keyed by prompt name    cli.py  entry point
profiles/        default.yaml (recipe + modes), internal_tool, customer_saas, dry_run
```

Terminology: a *role* is who an agent is; a *task* (tasks.py) is a reusable step such as "build"
or "verify"; a *prompt* is the model-facing part of a task; a *spec task* (`state.Task`, ids like
T1) is a concrete piece of the project being built.

## Invariants: do not break these

- **All model calls go through `Agent.ask` -> `MeteredLLM.call_json` -> backend.** In real runs the backend is `ModelRouter`, which reads the current profile on every call. An agent's `tier` is a tier name (cheap | mid | strong) or a pinned model spec from `agents.<name>.model`, and the router resolves both. Never hardcode a model id or provider in agents or tasks.
- **Token-spending tools** call `llm.reserve("tools", estimate)` before and `llm.record_external(...)` after.
- **Prompts live only in `tasks.py` and system prompts only in `roles.py`.** Agents and the Factory never hardcode prompt text. `{guidance}` in a prompt is filled automatically from `profile.guidance.<agent>`.
- **Each task is owned by exactly one agent** (enforced by tests). Cross-agent work goes through `agent.colleague(name).do(task)`, as when `write_spec` calls the critic.
- **Model outputs are single JSON objects.** Schemas live in `Prompt.output_schema`. When you change one, update the parsing in the task procedure and the script in `factory/mock.py` in the same change.
- **Deterministic before LLM:** checks run before any review, and review only runs per `stages.qa.llm_review` (always | risky | never). Keep the review diff-based.
- **Acceptance tests are protected:** once `state.acceptance_done`, `apply_edits` refuses edits under `tests/acceptance/`. Never let the builder weaken or delete them.
- **The human controls tools:** `tools.denied` and "never" answers override `tools.enabled`. Tools are only callable when enabled AND on the agent's `AgentSpec.tools` list.
- **Everything stays in the master folder:** write through `ArtifactStore` (or inside `store.app`). Checks must not leave caches in `app/`; that is why they run with `PYTHONDONTWRITEBYTECODE`, `--no-cache`, and so on.
- **Runs are resumable.** State is saved after planning, acceptance tests, each task, integration, handoff and deploy. A resumed run must not re-spend tokens on finished work (tested).
- **`task_result` telemetry fields are training data for a future learned router:** add fields, never rename.
- **Deploy commands only come from the profile** (run with the shell in `app/`), never from model output.

## Common changes

- **New specialist:** add a `Role(kind="specialist", default_tier=...)` in roles.py section 2 and list it in `specialists.allowed`.
- **New task:** in tasks.py, add a section with a `Prompt` (if it calls the model) and a procedure `def my_task(agent, ...)`. Register both in section 13. Assign the task to one agent in `AGENTS`, call it from the Factory, add a mock script, and add a budget stage if it uses a new `Prompt.stage`.
- **New model tool:** add a function `(args, providers) -> ToolResult` and a `Tool(...)` in tools.py section 1, with `est_tokens_per_call` set to 0 if it is local. Add the tool's name to the `tools` of the agents that may use it. Network or credential access goes through `providers` (wired in cli.py) so tests can mock it.
- **New deterministic check:** add it to `CHECKS` (subprocess) or `INTERNAL_CHECKS` (in-process) in tools.py section 4, and to the modes that should use it.
- **New mode or mode tweak:** edit `modes:` in `profiles/default.yaml`. Keep it a diff from the defaults. `tests/test_registries.py` resolves every mode.
- **New model or provider:** a new model on a supported provider needs no code. Use `provider:id` in a model set or override, and add its price under `pricing:`. For a new OpenAI-compatible endpoint, add an entry under `providers:` with `type: openai_compatible`. For a genuinely new API, add a backend class with `complete(system, prompt, model, max_tokens) -> LLMResponse` to `PROVIDER_TYPES` and `PACKAGES` in `factory/models.py`, plus a test with the SDK mocked.
- **New profile setting:** add a default to `profiles/default.yaml` first and read it with `.get(key, default)`.
- **Switching to LangGraph:** Factory stage methods map to nodes, and the pass/fail branch in `_run_task` maps to a conditional edge. The invariants still apply.

## Gotchas

- Anthropic model ids match the published list. The OpenAI and Gemini ids follow their published names but have not been used in a real run yet, so check them if a call fails. `web_search` uses the Anthropic server tool type `web_search_20250305` via `AnthropicWebSearch`; it needs `ANTHROPIC_API_KEY` whatever the model set.
- Non-Anthropic backends ask for JSON output natively (`json_mode`). Local servers often do not support it, so the `local` provider sets `json_mode: false` and `max_tokens_param: max_tokens`.
- `cost_calculator.html` in the repo root has its own copy of the prices. Keep it in sync with `pricing:` when prices change.
- Budget checks use a rough estimate (about 4 characters per token plus a quarter of `max_tokens`). The ledger records real usage.
- Each tool round re-sends the whole prompt, so keep `tools.max_rounds_per_call` at 1 unless there is a reason.
- Acceptance test files must be named `tests/acceptance/test_<criterion id>.py`, because QA uses the file name to decide which tests should already pass for a task.
- `ScriptedHumanChannel` defaults:
  - approvals: "approve"
  - readbacks: "y"
  - tool suggestions: "not now"
  - non-blocking requests (mode confirmation, critic issues, assumptions): accept the default
- `MockBackend` repeats the last script entry once a script runs out. The mock `build` script starts with a tool round.
- Mode is saved in `factory_state/state.json`. A resumed run keeps its mode unless `--mode` is given.
- Human-facing text (docs, prompts shown to the user) should avoid em-dashes; the owner prefers commas, colons or separate sentences.

## Owner context

The owner codes primarily in Python and revises these files by hand, so keep each concern in its
own clearly sectioned place, keep the section index at the top of each agent file accurate, keep
dependencies minimal, and update README.md and this file when structure or behavior changes.
