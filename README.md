# Software Factory

An agentic "software factory": one general recipe that turns an idea into delivered software,
adapting its depth to the project (simple tool, prototype or full-stack app). A cheap generalist
does most of the work; specialists, stronger models and extra quality steps are used only where
they pay off. You are interviewed up front, asked when it matters, and every token is budgeted.

```
idea ─► classify mode ─► interview ─► readback ─► spec ─► critic ─► sign-off (+ cost estimate)
                                                                         │
        ┌────────────── per task ───────────────┐                        ▼
        │  build ─► checks (free) ─► diff review │ ◄── acceptance tests (test-first, fullstack)
        │    ▲         fail │   (only if risky)  │
        │    └── escalate ◄─┘                    │
        └───────────────────────────────────────┘
                 ─► integration check ─► README handoff ─► deploy ─► monitor ─► PROJECT.md (+ dashboard)
```

## Quick start

```bash
pip install -r requirements.txt

# Which LLMs will be used, and are the keys and packages ready?
python -m factory models
python -m factory models --models openai

# Dry run: scripted model, auto-answers, no API key, no tokens
python -m factory run --idea "tiny calculator" --profile profiles/dry_run.yaml --mock --auto-approve

# Real run: you are interviewed and asked for approvals
export ANTHROPIC_API_KEY=...        # Windows PowerShell: $env:ANTHROPIC_API_KEY="..."
python -m factory run --idea-file idea.md                    # planner suggests a mode, you confirm
python -m factory run --idea-file idea.md --mode fullstack   # or choose it yourself

# Resume a stopped project (finished work is not redone)
python -m factory run --project 2026-09-26_quote_api

# Dashboard on demand, even when it is switched off for the mode
python -m factory dashboard --project 2026-09-26_quote_api
```

## One master folder per project

Every run creates (or resumes) one folder under the workspace (`~/factory_projects` by default,
`--workspace` to change). Everything the process creates goes in there:

```
~/factory_projects/2026-09-26_quote_api/
  PROJECT.md          status, tasks, where things are, how to resume, token usage
  dashboard.html      only when outputs.dashboard is on
  app/                the software itself (with its README.md and tests)
  docs/               idea, interview, spec, decisions, your feedback, acceptance tests, tool suggestions
  factory_state/      machine state: spec.json, state, ledger, telemetry, tool policy, resolved profile
```

The folder name defaults to `<date>_<first words of the idea>`; use `--project` to choose it.

## Switching LLMs

Which models the factory uses is configuration only (see `factory/models.py` and the top of
`profiles/default.yaml`). There are three levels, from coarse to fine:

1. **Model set:** one line switches every tier. The built-in sets are `anthropic` (the default), `anthropic_lean`, `anthropic_max`, `openai`, `gemini` and `mixed_budget`.
   ```yaml
   model_set: openai
   ```
2. **Per tier:** override one tier on top of the set.
   ```yaml
   models:
     strong: anthropic:claude-opus-5-5     # OpenAI for the rest, Claude for planning and escalations
   ```
3. **Per agent:** pin one agent to a specific model.
   ```yaml
   agents:
     reviewer: {model: gemini:gemini-3.1-pro}
   ```

The same from the command line, without editing files:

```bash
python -m factory run --models gemini --model strong=anthropic:claude-opus-5-5 --model reviewer=openai:gpt-5.6-terra --idea-file idea.md
```

A model is written as `provider:model_id`. A bare id means Anthropic. Supported providers:

- **Anthropic, OpenAI and Gemini:** keys come from `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `GEMINI_API_KEY`.
- **Any OpenAI-compatible server:** for example Ollama, vLLM or LM Studio for local models, used as `local:<model>`. You can add more under `providers:`.

Before a real run the factory checks that every provider in use has its SDK installed and its key set, and stops with a clear message if not. It does this before creating folders or spending tokens. `PROJECT.md` shows the models used and an estimated dollar cost, based on the `pricing:` table in the profile.

`cost_calculator.html` (open it in a browser) compares costs for every mode and model combination and gives you the matching `model_set` line or command.

Caveats:
- The prompts were written with Claude in mind, so other models may need more JSON retries.
- Web search uses Anthropic's search tool, so it only works when `ANTHROPIC_API_KEY` is set.
- Verify the OpenAI and Gemini model ids against each provider's docs before a real run.

## Build modes

The planner suggests a mode from your idea and you confirm or change it (`--mode` skips this).
Modes are defined in `profiles/default.yaml` under `modes:` and only list what differs.

| | simple | prototype | fullstack |
|---|---|---|---|
| For | scripts, CLIs, small tools | demos, proofs of concept | production apps with several components |
| Interview | 2 rounds | 3 rounds | 4 rounds |
| Spec critic | off | on (3 issues max) | on |
| Acceptance tests first | off | off | on |
| Checks | compile, pytest | + ruff | + mypy, bandit |
| LLM review | never | never | only risky tasks or big diffs |
| Specialists | off | off | on |
| Deploy / monitor | off | off | on |
| Dashboard | off | off | on |
| Build budget | 120k tokens | 250k tokens | 800k tokens |

Layering: `default.yaml` < mode overrides < your project profile, so anything you set yourself wins.
Examples: `profiles/internal_tool.yaml` (forces simple), `profiles/customer_saas.yaml` (forces
fullstack with stricter review and Docker deploy).

## Where things live

Everything about agents is in `factory/agents/`, one file per concern:

| File | Holds | Edit it to... |
|---|---|---|
| `agents.py` | All agents (role + tasks + tools), routing, and the pipeline between them | change who does what, when, with which model |
| `roles.py` | All roles: system prompts for core roles and specialists | change how an agent thinks, add a specialist |
| `tasks.py` | All tasks: each task's prompt next to its procedure | change what is asked, returned or done in a step |
| `tools.py` | All tools: model tools, file/context/check/shell tools, ToolBroker | add a tool or a check, change tool policy |

Agents are declarative: `AgentSpec("builder", role="builder", tasks=("build",), tools=(...))`.
The rest of `factory/` is infrastructure that these files reference: models (which LLMs, providers,
pricing), budget, LLM metering, human loop, decisions, knowledge (lessons), state, artifacts
(master folder), telemetry, dashboard.

## Quality features and what they cost

| Feature | What it does | Token cost |
|---|---|---|
| Mode classification | Picks the lightest mode that fits, so small ideas do not pay for full-stack ceremony | ~300 tokens, cheap model |
| Interview + readback | Shared understanding before any code | Strong model, but only in planning |
| Spec critic | Cheap reviewer finds ambiguity, missing edge cases, untestable criteria; you answer, the spec is redrafted once | Cheap model; one redraft only if you answered |
| Cost estimate at sign-off | Shows expected tokens (from past projects' averages) next to the budget | Zero |
| Acceptance tests first | Test engineer writes one test file per criterion before code; builder cannot edit them, so it cannot "pass" by weakening tests | One mid-tier call per project |
| Scoped acceptance runs | Each task only runs acceptance tests for criteria that should already work | Zero |
| Compile check | In-process syntax check, fastest failure signal | Zero |
| Diff-based review | Reviewer sees only the diff, and only for risky tasks or big diffs (`llm_review: risky`) | Much smaller prompts; often skipped |
| No-progress detection | Empty answers and repeated failures are fingerprinted; escalation and early stop | Saves wasted loops |
| Lessons across projects | Problems that needed retries, and decisions you rejected, are stored per project and reused in similar tasks in later projects | Zero to find, a few hundred tokens to include |
| Integration check | Full suite once at the end, with one targeted fix attempt | Zero unless a fix is needed |
| Handoff README | README written from spec and repo map, never from full code | One cheap call |
| PROJECT.md | Status, tasks and usage summary for the master folder | Zero |

Plus everything from before: token ledger per stage, context diet (repo map plus relevant files
only), search/replace edits, prompt caching, tool budget, loop caps, specialist and model
escalation only on triggers.

## Honesty and human input

- **Questions:** agents must ask when they would otherwise invent a requirement. Blocking questions stop work; non-blocking ones proceed on a stated assumption and come to you in a batch. Anything you overrule becomes authoritative feedback for every later prompt.
- **Decisions:** decisions are classified by reversibility and category. Costly, architectural, scope-changing or risk-tagged ones need your approval, depending on `human.autonomy`.
- **Tools:** agents may suggest missing tools with a reason, benefit and cost. You choose "enable for this project", "not now" or "never", and `tools.denied` overrides everything.

## Extending

- **New specialist:** add a `Role(kind="specialist", ...)` in `roles.py` and list it in `specialists.allowed`.
- **New task:** add a section to `tasks.py` (Prompt + procedure + `TaskDef`), give it to an agent in `agents.py`, and add a mock script in `factory/mock.py`.
- **New tool:** add a function and `Tool(...)` in `tools.py` (section 1), then allow it on an agent in `agents.py`.
- **New check:** add it to `CHECKS` (or `INTERNAL_CHECKS`) in `tools.py` section 4.
- **New mode:** add it under `modes:` in `profiles/default.yaml`.
- **LangGraph:** each `Factory._plan / _run_task / _integration / _deploy` maps to a node.

Model names in `profiles/default.yaml` are placeholders to verify against the current Anthropic
model list before a real run.

## Tests

```bash
python -m pytest -q
```

The suite has 57 tests, covering:
- routing
- model sets, provider routing, backend request shapes (with the SDKs mocked) and cost estimates
- file, check and model tools
- the tool broker
- config layering
- lessons
- registry consistency, meaning every agent's roles, tasks and tools exist and every task has exactly one owner
- end-to-end mock runs for full-stack mode (every quality step) and auto mode (prototype, light steps)
- resume without token spend
- a critic-driven redraft
