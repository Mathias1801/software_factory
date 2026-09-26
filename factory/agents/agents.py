"""ALL AGENTS, and the logistics between them.

  1. AgentSpec + AGENTS   who exists: a role (roles.py) + the tasks it may run (tasks.py)
                          + the model tools it may call (tools.py)
  2. Agent                runtime: ask() renders a prompt with the agent's role, guidance and tools,
                          runs tool rounds; do() runs one of the agent's tasks; colleague() for hand-offs
  3. Routing              which builder (generalist or specialist) and which model tier per attempt
  4. Factory              the pipeline and its logistics:
                          plan -> acceptance tests -> build/verify loop per task -> integration
                          -> handoff -> deploy -> monitor -> PROJECT.md (+ optional dashboard)

Everything an agent says lives in roles.py and tasks.py; everything it can do without a model lives
in tools.py. This file only decides who does what, when, with which budget and model."""
from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml

from factory.agents.roles import get_role, specialist_names
from factory.agents.tasks import PROMPTS, TASKS, QAResult
from factory.agents.tools import ToolBroker, snapshot_files, trim
from factory.artifacts import ArtifactStore
from factory.budget import BudgetExceeded, TokenLedger
from factory.config import ProfileLayers, resolve
from factory.dashboard import build_dashboard
from factory.decisions import DecisionLog
from factory.errors import FactoryStopped
from factory.human import HumanChannel, HumanLoop, HumanRequest
from factory.knowledge import Knowledge
from factory.llm import LLMBackend, MeteredLLM
from factory.models import ModelRouter, cost_by_stage, effective_models
from factory.state import RunState, Spec, Task
from factory.telemetry import Telemetry


# =============================================================================
# 1. AGENT SPECS
# =============================================================================
@dataclass(frozen=True)
class AgentSpec:
    name: str
    role: str  # key in roles.ROLES
    tasks: tuple[str, ...]  # keys in tasks.TASKS
    tools: tuple[str, ...] = ()  # model tools (tools.TOOLS) this agent may call, if the human allows
    description: str = ""


AGENTS: dict[str, AgentSpec] = {a.name: a for a in [
    AgentSpec("planner", "planner", ("classify_mode", "interview", "readback", "write_spec"),
              ("web_search", "statistics", "expenses"), "Turns an idea into an approved spec"),
    AgentSpec("critic", "critic", ("critique_spec",), (), "Finds rework risks in the spec before sign-off"),
    AgentSpec("test_engineer", "test_engineer", ("acceptance_tests",), (), "Writes acceptance tests first"),
    AgentSpec("builder", "builder", ("build",), ("statistics", "expenses", "web_search"),
              "Implements one task at a time; can take on a specialist role"),
    AgentSpec("reviewer", "reviewer", ("verify", "integration_check"), (), "Runs checks and reviews diffs"),
    AgentSpec("release_manager", "release_manager", ("handoff", "deploy", "monitor"), (),
              "Documents, deploys and monitors the app"),
]}


# =============================================================================
# 2. AGENT RUNTIME
# =============================================================================
class Agent:
    def __init__(self, spec: AgentSpec, factory: Factory):
        self.spec = spec
        self.factory = factory

    # shared services, always read through the factory so mode changes apply everywhere
    name = property(lambda self: self.spec.name)
    profile = property(lambda self: self.factory.profile)
    store = property(lambda self: self.factory.store)
    human = property(lambda self: self.factory.human)
    telemetry = property(lambda self: self.factory.telemetry)
    llm = property(lambda self: self.factory.llm)
    tools = property(lambda self: self.factory.tools)
    decisions = property(lambda self: self.factory.decisions)
    knowledge = property(lambda self: self.factory.knowledge)

    @property
    def tier(self) -> str:
        """A tier name, or a model spec when the profile pins this agent to a model (agents.<name>.model)."""
        cfg = self.profile.get("agents", {}).get(self.name, {}) or {}
        return cfg.get("model") or cfg.get("tier") or get_role(self.spec.role).default_tier or "cheap"

    @property
    def guidance(self) -> str:
        return str(self.profile.get("guidance", {}).get(self.name, "")).strip()

    def colleague(self, name: str) -> Agent:
        return self.factory.agents[name]

    def do(self, task_name: str, *args, **kwargs):
        if task_name not in self.spec.tasks:
            raise ValueError(f"Agent '{self.name}' is not assigned task '{task_name}'")
        return TASKS[task_name].run(self, *args, **kwargs)

    def ask(self, prompt_name: str, *, tier: str | None = None, system: str | None = None,
            as_agent: str | None = None, max_tokens: int | None = None, **values) -> dict:
        """Render a prompt, call the model, run tool rounds, collect tool suggestions."""
        prompt = PROMPTS[prompt_name]
        values.setdefault("guidance", self.guidance or "(none)")
        system = system or get_role(self.spec.role).system_prompt
        use_tools = prompt.uses_tools and bool(self.spec.tools)
        text = prompt.render(self.tools.prompt_section(self.spec.tools) if use_tools else "", **values)
        label = as_agent or self.name
        rounds = self.tools.max_rounds if use_tools else 0
        data: dict = {}
        for r in range(rounds + 1):
            data = self.llm.call_json(stage=prompt.stage, purpose=prompt_name, system=system, prompt=text,
                                      tier=tier or self.tier, agent=label, max_tokens=max_tokens)
            if use_tools:
                self.tools.collect_suggestions(data.get("tool_suggestions"), stage=prompt.stage, agent=label)
            calls = data.get("tool_calls") or []
            if not use_tools or not calls or r == rounds:
                return data
            results = self.tools.run_calls(calls, stage=prompt.stage, agent=label, allowed=self.spec.tools)
            text += ("\n\nTool results (use them; do not repeat the same call). Now give your full answer "
                     "without tool_calls:\n" + results)
        return data


# =============================================================================
# 3. ROUTING
# =============================================================================
@dataclass
class TaskContext:
    domain_tags: set[str]
    risk_tags: set[str]
    failure_count: int
    budget_left_tokens: int | None


def pick_builder(ctx: TaskContext, profile: dict) -> str:
    """'builder' by default; 'specialist:<name>' only when a rule says it pays off."""
    spec_cfg = profile.get("specialists", {})
    if not spec_cfg.get("enabled", True):
        return "builder"
    if ctx.budget_left_tokens is not None and ctx.budget_left_tokens < int(spec_cfg.get("estimated_tokens", 40000)):
        return "builder"
    matching = ctx.domain_tags & set(spec_cfg.get("allowed", [])) & set(specialist_names())
    if not matching:
        return "builder"
    esc = profile.get("escalation", {})
    if ctx.risk_tags & set(esc.get("on_risk_tags", [])) or ctx.failure_count >= int(esc.get("on_repeated_failure", 2)):
        return f"specialist:{min(matching)}"
    return "builder"


def pick_tier(as_agent: str, failure_count: int, profile: dict) -> str:
    if as_agent.startswith("specialist:"):
        tier = get_role(as_agent.split(":", 1)[1]).default_tier
        if tier:
            return tier
    base = profile.get("agents", {}).get("builder", {}).get("tier") or get_role("builder").default_tier or "cheap"
    if failure_count >= int(profile.get("escalation", {}).get("strong_model_after", 3)):
        return "strong"
    return base


class FailureTracker:
    """Counts how often the same failure fingerprint repeats for a task."""

    def __init__(self):
        self.counts: defaultdict[tuple[str, str], int] = defaultdict(int)

    def record(self, task_id: str, fingerprints: list[str]) -> int:
        worst = 0
        for fp in fingerprints:
            self.counts[(task_id, fp)] += 1
            worst = max(worst, self.counts[(task_id, fp)])
        return worst

    def max_repeat(self, task_id: str) -> int:
        return max((c for (t, _), c in self.counts.items() if t == task_id), default=0)


# =============================================================================
# 4. FACTORY (pipeline and logistics)
# =============================================================================
class Factory:
    def __init__(self, layers: ProfileLayers, project_dir: str | Path, backend: LLMBackend | None, channel: HumanChannel,
                 providers: dict | None = None, mode: str | None = None):
        """backend=None uses the ModelRouter (real providers from the profile); tests pass MockBackend."""
        self.layers = layers
        self.store = ArtifactStore(project_dir)
        self.telemetry = Telemetry(self.store.path("telemetry.jsonl"))
        self.state = RunState.load(self.store)
        chosen = mode or self.state.mode or layers.mode()
        self.profile = resolve(layers, None if chosen == "auto" else chosen)
        budget_cfg = self.profile["budget"]
        self.ledger = TokenLedger(budget_cfg["stage_tokens"], budget_cfg.get("warn_at", 0.8))
        self.ledger.load(self.store.read_json("ledger.json") or {})
        self.human = HumanLoop(channel, self.telemetry)
        backend = backend or ModelRouter(lambda: self.profile)
        self.llm = MeteredLLM(backend, self.ledger, self.telemetry, on_budget_exceeded=self._on_budget_exceeded,
                              extend_factor=float(budget_cfg.get("extend_factor", 1.5)))
        self.tools = ToolBroker(self.profile, self.store, self.human, self.telemetry, self.llm, providers)
        self.decisions = DecisionLog(self.store)
        self.knowledge = Knowledge(self.store)
        self.agents = {name: Agent(spec, self) for name, spec in AGENTS.items()}
        if chosen != "auto" and self.state.mode != chosen:
            self._apply_mode(chosen)

    # ------------------------------------------------------------------ public
    def run(self, idea: str | None = None) -> dict:
        if idea and not self.store.path("idea.md").exists():
            self.store.write_text("idea.md", f"# Idea\n\n{idea.strip()}\n")
        self.telemetry.log("run_start", project=self.store.root.name, mode=self.state.mode)
        outcome = "completed"
        try:
            spec = self._plan(idea)
            self._acceptance(spec)
            self._build_all(spec)
            self._integration(spec)
            self._handoff(spec)
            self._deploy(spec)
        except FactoryStopped as exc:
            outcome = f"stopped: {exc}"
            self.telemetry.log("run_stopped", reason=str(exc))
        finally:
            self._save()
        self._monitor()
        dashboard = self.build_dashboard() if self.profile.get("outputs", {}).get("dashboard", False) else None
        self._write_project_md(outcome)
        self.telemetry.log("run_end", outcome=outcome, tokens=self.ledger.total_used())
        return {
            "outcome": outcome, "mode": self.state.mode, "master_folder": str(self.store.root),
            "app": str(self.store.app), "completed_tasks": self.state.completed, "skipped_tasks": self.state.skipped,
            "integration_passed": self.state.integration_passed, "deployed": self.state.deployed,
            "tokens": self.ledger.report(), "dashboard": str(dashboard) if dashboard else None,
        }

    def build_dashboard(self) -> Path:
        return build_dashboard(self.store, self.telemetry, self.ledger)

    # ------------------------------------------------------------------ modes
    def _apply_mode(self, mode: str) -> None:
        new = resolve(self.layers, mode)
        self.profile.clear()
        self.profile.update(new)
        for stage, budget in new["budget"]["stage_tokens"].items():
            self.ledger.budgets[stage] = int(budget)
        self.tools.reload(self.profile)
        self.state.mode = mode
        self.store.write_text("profile_resolved.yaml", yaml.safe_dump(copy.deepcopy(self.profile), sort_keys=False))
        self.telemetry.log("mode_applied", mode=mode)
        self._save()

    # ------------------------------------------------------------------ stages
    def _plan(self, idea: str | None) -> Spec:
        existing = self.store.read_json("spec.json")
        if existing and self.state.spec_done:
            return Spec.from_dict(existing)
        if not idea:
            raise FactoryStopped("No idea given. Pass --idea or --idea-file to start a new project.")
        planner = self.agents["planner"]
        if not self.state.mode:
            mode, _ = planner.do("classify_mode", idea)
            self._apply_mode(mode)
        interview_cfg = self.profile["human"].get("interview", {})
        transcript = planner.do("interview", idea) if interview_cfg.get("enabled", True) else []
        understanding = planner.do("readback", idea, transcript) if interview_cfg.get("require_readback", True) else ""
        self.store.write_json("interview.json", {"idea": idea, "transcript": transcript, "readback": understanding})
        self.store.write_text("interview.md", "# Interview\n\n" + "\n\n".join(
            f"**Q ({t['theme']}):** {t['q']}\n\n**A:** {t['a']}" for t in transcript)
            + f"\n\n## Agreed understanding\n\n{understanding or '(no readback)'}\n")
        spec = planner.do("write_spec", idea, transcript, understanding, estimate=self._estimate)
        self.state.spec_done = True
        self._save()
        self._flush("plan")
        return spec

    def _acceptance(self, spec: Spec) -> None:
        if self.profile.get("quality", {}).get("test_first") and not self.state.acceptance_done:
            self.agents["test_engineer"].do("acceptance_tests", spec)
            self.state.acceptance_done = True
            self._save()

    def _build_all(self, spec: Spec) -> None:
        per_task = self.profile["human"].get("question_batching", "per_stage") == "per_task"
        for task in spec.tasks:
            if task.id in self.state.completed or task.id in self.state.skipped:
                continue
            self._run_task(task, spec)
            self._save()
            if per_task:
                self._flush("build")
        self._flush("build")

    def _integration(self, spec: Spec) -> None:
        if not self.profile.get("quality", {}).get("integration_check", True) or self.state.integration_passed:
            return
        reviewer = self.agents["reviewer"]
        result: QAResult = reviewer.do("integration_check")
        if not result.passed and "INT" not in self.state.completed and "INT" not in self.state.skipped:
            fix = Task(id="INT", title="Fix integration failures",
                       description="Every task passed on its own, but the full check suite fails. Fix the failures "
                                   "without changing behaviour the spec requires.")
            self._run_task(fix, spec, initial_feedback=result.report, initial_fingerprints=result.fingerprints)
            result = reviewer.do("integration_check")
        self.state.integration_passed = result.passed
        self._save()

    def _handoff(self, spec: Spec) -> None:
        if self.profile.get("quality", {}).get("handoff_readme", True) and not self.state.handoff_done:
            self.agents["release_manager"].do("handoff", spec)
            self.state.handoff_done = True
            self._save()

    def _deploy(self, spec: Spec) -> None:
        if not self.profile["stages"]["deploy"].get("enabled", True) or self.state.deployed:
            return
        if self.state.skipped:
            ans = self.human.ask(HumanRequest(kind="approval", stage="deploy", options=["approve", "reject"],
                                              summary=f"Tasks were skipped ({', '.join(self.state.skipped)}). Deploy anyway?"))
            if ans.strip().lower() not in {"approve", "y", "yes"}:
                return
        self.state.deployed = self.agents["release_manager"].do("deploy", spec).ok

    def _monitor(self) -> None:
        if self.profile["stages"]["monitor"].get("enabled", False):
            self.agents["release_manager"].do("monitor")

    # --------------------------------------------------------------- task loop
    def _allowed_criteria(self, spec: Spec, task: Task) -> set[str]:
        ids = {c["id"].upper() for c in spec.criteria_for(task)}
        for done in spec.tasks:
            if done.id in self.state.completed:
                ids |= {c["id"].upper() for c in spec.criteria_for(done)}
        return ids

    def _run_task(self, task: Task, spec: Spec, loops: int | None = None, is_retry: bool = False,
                  initial_feedback: str = "", initial_fingerprints: list[str] | None = None) -> None:
        qa_cfg = self.profile["stages"]["qa"]
        quality = self.profile.get("quality", {})
        max_loops = loops or int(qa_cfg.get("max_loops", 4))
        max_same = int(self.profile["budget"].get("max_same_failure", 2))
        tracker = FailureTracker()
        tokens_before = self.ledger.used["build"] + self.ledger.used["qa"]
        snapshot = snapshot_files(self.store.app)
        allowed = self._allowed_criteria(spec, task)
        lessons = ""
        if quality.get("lessons", True):
            lessons = self.knowledge.lessons_for(f"{task.title} {task.description}", task.domain_tags + task.risk_tags,
                                                 limit=int(quality.get("max_lessons", 3)))
        failure_feedback, fingerprints = initial_feedback, list(initial_fingerprints or [])
        first_failure, touched = "", []
        as_agent, tier = "builder", pick_tier("builder", 0, self.profile)
        builder, reviewer = self.agents["builder"], self.agents["reviewer"]

        for attempt in range(1, max_loops + 1):
            failures = tracker.max_repeat(task.id)
            ctx = TaskContext(set(task.domain_tags), set(task.risk_tags), failures, self.ledger.remaining("build"))
            as_agent = pick_builder(ctx, self.profile)
            tier = pick_tier(as_agent, failures, self.profile)
            if as_agent != "builder" or tier != pick_tier("builder", 0, self.profile):
                self.telemetry.log("escalation", task_id=task.id, attempt=attempt, agent=as_agent, tier=tier,
                                   failure_count=failures)
            build = builder.do("build", task, spec, as_agent=as_agent, tier=tier, failure_feedback=failure_feedback,
                               fingerprints=fingerprints, extra_files=touched, lessons=lessons)
            touched = sorted(set(touched) | set(build.applied_files))
            if not build.ok:
                failure_feedback = "\n".join(build.edit_errors + build.retry_reasons)
                if build.edit_errors:
                    fingerprints = ["edit|" + e.split(":")[0] for e in build.edit_errors]
                    tracker.record(task.id, fingerprints)
                continue
            if not build.applied_files:
                fingerprints = ["no_edits"]
                tracker.record(task.id, fingerprints)
                failure_feedback = "Your previous answer contained no edits. Make the changes the task needs."
                continue

            result = reviewer.do("verify", task, spec, touched, snapshot, allowed)
            if result.passed:
                if attempt > 1 and quality.get("lessons", True) and first_failure:
                    self.knowledge.record_lesson(problem=first_failure, fix=build.notes or "Fixed after retry",
                                                 tags=task.domain_tags + task.risk_tags,
                                                 context=f"{task.title} {task.description}")
                self._task_done(task, "passed", attempt, as_agent, tier, tokens_before)
                return
            first_failure = first_failure or f"{', '.join(result.fingerprints[:3])}: {trim(result.report, 300)}"
            fingerprints = result.fingerprints
            repeat = tracker.record(task.id, fingerprints)
            failure_feedback = result.report
            if repeat > max_same and tier == "strong":
                self.telemetry.log("early_stop", task_id=task.id, reason="same failure after escalation")
                break

        self._task_done(task, "failed", max_loops, as_agent, tier, tokens_before)
        ans = self.human.ask(HumanRequest(
            kind="approval", stage="qa", summary=f"Task {task.id} ('{task.title}') is still failing. What should happen?",
            context=trim(failure_feedback, 3000),
            options=["retry with 2 more attempts", "skip this task", "stop the run"],
        )).strip().lower()
        if ans.startswith("retry") and not is_retry:
            return self._run_task(task, spec, loops=2, is_retry=True, initial_feedback=failure_feedback,
                                  initial_fingerprints=fingerprints)
        if ans.startswith("stop"):
            raise FactoryStopped(f"Stopped by human at task {task.id}")
        self.state.skipped.append(task.id)

    def _task_done(self, task: Task, outcome: str, attempts: int, as_agent: str, tier: str, tokens_before: int) -> None:
        if outcome == "passed":
            self.state.completed.append(task.id)
        spent = self.ledger.used["build"] + self.ledger.used["qa"] - tokens_before
        # These features are the training data for a future learned router. Add fields, never rename.
        self.telemetry.log(
            "task_result", task_id=task.id, outcome=outcome, attempts=attempts,
            first_pass=(outcome == "passed" and attempts == 1), final_agent=as_agent, final_tier=tier,
            tokens_spent=spent, domain_tags=task.domain_tags, risk_tags=task.risk_tags, mode=self.state.mode,
            n_criteria=len(task.acceptance_ids), n_files_hint=len(task.files_hint), desc_chars=len(task.description),
        )

    # ----------------------------------------------------------------- helpers
    def _estimate(self, spec: Spec) -> str:
        avg, n = self.knowledge.avg_task_tokens()
        per_task = avg or float(self.profile.get("quality", {}).get("default_tokens_per_task", 30000))
        basis = f"average of {n} past tasks" if avg else "default per-task estimate"
        budgets = self.ledger.budgets
        budget = sum(budgets.get(s, 0) for s in ("tests", "build", "qa"))
        return (f"\n**Estimated cost:** about {int(per_task * len(spec.tasks)):,} tokens for {len(spec.tasks)} "
                f"tasks ({basis}). Budget for tests, build and QA: {budget:,} tokens. Mode: {self.state.mode}.\n")

    def _flush(self, stage: str) -> None:
        self.tools.review_suggestions()
        for req in self.human.flush(stage):
            self.store.append_text("human_feedback.md",
                                   f"- Overruled assumption ({req.stage}): '{req.default_assumption}' -> {req.answer}\n")

    def _on_budget_exceeded(self, err: BudgetExceeded) -> bool:
        mode = self.profile["budget"].get("on_exceeded", "ask_human")
        if mode == "continue":
            return True
        if mode == "stop":
            raise FactoryStopped(str(err))
        ans = self.human.ask(HumanRequest(
            kind="approval", stage=err.stage, options=["continue with a larger budget", "stop"],
            summary=f"The {err.stage} stage is about to exceed its token budget ({err.projected:,}/{err.budget:,}).",
        )).strip().lower()
        if ans.startswith("continue") or ans in {"approve", "y", "yes"}:
            return True
        raise FactoryStopped(f"Budget for {err.stage} not extended.")

    def _save(self) -> None:
        self.state.save(self.store)
        self.store.write_json("ledger.json", self.ledger.to_dict())

    def _write_project_md(self, outcome: str) -> None:
        spec_data = self.store.read_json("spec.json")
        spec = Spec.from_dict(spec_data) if spec_data else None
        rows = []
        for t in spec.tasks if spec else []:
            status = "done" if t.id in self.state.completed else "skipped" if t.id in self.state.skipped else "open"
            rows.append(f"| {t.id} | {t.title} | {status} |")
        costs, unpriced = cost_by_stage(self.telemetry.read("llm_call"), self.profile.get("pricing", {}))
        tokens = "\n".join(
            f"| {s} | {v['used']:,} | {v['budget']:,} | {'$%.2f' % costs[s] if s in costs else '-'} |"
            for s, v in self.ledger.report().items() if v["used"])
        try:
            models = ", ".join(f"{t}: `{m}`" for t, m in effective_models(self.profile).items())
        except ValueError:
            models = "(not configured)"
        cost_note = f"**Estimated cost:** ${sum(costs.values()):.2f}" + (
            f" (no price for: {', '.join(sorted(unpriced))})" if unpriced else "")
        integ = {True: "passed", False: "FAILED", None: "not run"}[self.state.integration_passed]
        dash = "- `dashboard.html`: factory and product health\n" if self.store.path("dashboard.html").exists() else ""
        self.store.write_text("PROJECT.md", f"""# {self.store.root.name}

{spec.summary if spec else "(no spec yet)"}

**Status:** {outcome} | **Mode:** {self.state.mode or "not chosen"} | **Integration check:** {integ} | **Deployed:** {"yes" if self.state.deployed else "no"}

## Tasks
| Task | Title | Status |
|---|---|---|
{chr(10).join(rows) or "| - | - | - |"}

## Where things are
- `app/`: the software (see `app/README.md` for how to run it)
- `docs/`: idea, interview, spec, decisions, your feedback, acceptance tests, tool suggestions
- `factory_state/`: machine state used to resume, plus telemetry and token ledger
{dash}
## Resume or continue
Run the same command again with `--project {self.store.root.name}`; finished work is not redone.

## Models and cost
Model set: `{self.profile.get("model_set", "-")}` ({models})

{cost_note}

| Stage | Tokens used | Budget | Est. cost |
|---|---|---|---|
{tokens or "| - | 0 | - | - |"}
""")
