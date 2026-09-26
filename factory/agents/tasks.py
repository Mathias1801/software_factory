"""ALL TASKS. Each task section holds its prompt(s) directly next to the procedure that uses them,
so one task can be revised in one place.

   0. Types              Prompt, TaskDef, result dataclasses
   1. classify_mode      suggest simple | prototype | fullstack; human confirms
   2. interview          rounds of questions until nothing blocking remains
   3. readback           restate the understanding; human confirms or corrects
   4. write_spec         draft -> critic pass -> cost estimate -> human sign-off
   5. critique_spec      cheap review of the spec for ambiguity, gaps, untestable criteria
   6. acceptance_tests   test-first: one executable test file per criterion (protected files)
   7. build              implement one spec task; decisions, questions, lessons
   8. verify             deterministic checks, then a diff-based review only when warranted
   9. integration_check  full check suite once every task has passed
  10. handoff            README for the delivered app
  11. deploy             approval, profile commands, rollback, manifest
  12. monitor            health checks
  13. Registries         PROMPTS and TASKS

A task procedure always receives the Agent running it as its first argument. Through it a task
reaches the model (agent.ask), the human, the store, tools and other agents (agent.colleague).
If you change an output schema, update the matching script in factory/mock.py too."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from factory.agents.roles import system_prompt_for
from factory.agents.tools import (
    apply_edits,
    diff_since,
    diff_size,
    file_bundle,
    files_from_fingerprints,
    http_health,
    repo_map,
    run_checks,
    run_commands,
    trim,
)
from factory.config import describe_modes
from factory.decisions import Decision, needs_approval
from factory.errors import FactoryStopped
from factory.human import YES, HumanRequest
from factory.state import Spec, Task

if TYPE_CHECKING:
    from factory.agents.agents import Agent

ACCEPTANCE_DIR = "tests/acceptance/"


# =============================================================================
# 0. TYPES
# =============================================================================
@dataclass(frozen=True)
class Prompt:
    name: str
    stage: str  # budget stage the tokens are charged to
    instructions: str  # str.format placeholders; {guidance} is filled automatically per agent
    output_schema: str
    uses_tools: bool = False

    def render(self, tools_section: str = "", **values) -> str:
        parts = [self.instructions.format(**values)]
        if self.uses_tools and tools_section:
            parts.append(tools_section)
        parts.append("Return a single JSON object in this shape:\n" + self.output_schema)
        return "\n\n".join(parts)


@dataclass(frozen=True)
class TaskDef:
    name: str
    description: str
    run: Callable[..., Any]
    prompts: tuple[str, ...] = ()


@dataclass
class BuildResult:
    applied_files: list[str] = field(default_factory=list)
    edit_errors: list[str] = field(default_factory=list)
    retry_reasons: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def ok(self) -> bool:
        return not self.edit_errors and not self.retry_reasons


@dataclass
class QAResult:
    passed: bool
    fingerprints: list[str] = field(default_factory=list)
    report: str = ""


@dataclass
class DeployResult:
    ok: bool
    skipped: bool
    detail: str


def _criteria_text(spec: Spec, task: Task) -> str:
    return "\n".join(f"- {c['id']}: {c['text']}" for c in spec.criteria_for(task))


def format_transcript(transcript: list[dict]) -> str:
    if not transcript:
        return "(no interview yet)"
    lines = []
    for t in transcript:
        answer = t["a"]
        if answer.strip().lower() == "skip":
            answer = "(skipped by human: record an explicit assumption)"
        lines.append(f"Q [{t['theme']}]: {t['q']}\nA: {answer}")
    return "\n".join(lines)


# =============================================================================
# 1. CLASSIFY_MODE
# =============================================================================
CLASSIFY_MODE = Prompt(
    name="classify_mode", stage="plan",
    instructions="""Project idea:
{idea}

Available build modes:
{modes}

Pick the lightest mode that can responsibly deliver this idea. Prefer "simple" for small scripts and
single-purpose tools, "prototype" for demos and proofs of concept, and "fullstack" only for
production-grade systems with several components (for example backend, frontend and database).""",
    output_schema='{"mode": str, "reason": str}',
)


def classify_mode(agent: Agent, idea: str) -> tuple[str, str]:
    modes = agent.factory.layers.modes()
    data = agent.ask("classify_mode", idea=idea, modes=describe_modes(modes), max_tokens=300)
    suggested = str(data.get("mode", "")).lower()
    if suggested not in modes:
        suggested = "prototype" if "prototype" in modes else next(iter(modes))
    reason = str(data.get("reason", ""))
    req = HumanRequest(
        kind="mode", stage="plan", summary=f"Suggested build mode: {suggested}",
        context=f"Why: {reason}\n\n{describe_modes(modes)}", options=list(modes),
        blocking=False, default_assumption=suggested,
    )
    answer = agent.human.ask_batch([req])[0].strip().lower()
    chosen = answer if answer in modes else suggested
    agent.telemetry.log("mode_selected", suggested=suggested, chosen=chosen, reason=reason)
    return chosen, reason


# =============================================================================
# 2. INTERVIEW
# =============================================================================
INTERVIEW_THEMES = "users_goals, features, constraints, integrations, done_definition"

INTERVIEW = Prompt(
    name="interview", stage="plan",
    instructions="""Project idea:
{idea}

Interview so far:
{transcript}

Mode guidance: {guidance}

This is round {round} of at most {max_rounds}. Ask the next round of at most {max_questions}
questions, using themes from: {themes}. Mark a question blocking=true only if a responsible spec
cannot be written without it. If you already have enough, set done=true.""",
    output_schema='{"done": bool, "questions": [{"q": str, "theme": str, "blocking": bool}]}',
)


def interview(agent: Agent, idea: str) -> list[dict]:
    cfg = agent.profile["stages"]["plan"]
    transcript: list[dict] = []
    max_rounds = int(cfg.get("max_interview_rounds", 4))
    max_q = int(cfg.get("max_questions_per_round", 5))
    for rnd in range(1, max_rounds + 1):
        data = agent.ask("interview", idea=idea, transcript=format_transcript(transcript), round=rnd,
                         max_rounds=max_rounds, max_questions=max_q, themes=INTERVIEW_THEMES, max_tokens=1500)
        questions = (data.get("questions") or [])[:max_q]
        if data.get("done") or not questions:
            break
        reqs = [HumanRequest(kind="question", stage="plan", summary=str(q.get("q", "")),
                             context=f"Theme: {q.get('theme', 'general')}. Type 'skip' to let the planner assume.")
                for q in questions]
        answers = agent.human.ask_batch(reqs)
        for q, a in zip(questions, answers, strict=True):
            transcript.append({"q": str(q.get("q", "")), "theme": str(q.get("theme", "general")),
                               "blocking": bool(q.get("blocking")), "a": a or "skip"})
        if not any(q.get("blocking") for q in questions):
            break
    return transcript


# =============================================================================
# 3. READBACK
# =============================================================================
READBACK = Prompt(
    name="readback", stage="plan",
    instructions="""Project idea:
{idea}

Interview:
{transcript}

Corrections from the human so far:
{corrections}

Restate your understanding in your own words, including what is explicitly OUT of scope.""",
    output_schema='{"readback": str, "in_scope": [str], "out_of_scope": [str]}',
)


def readback(agent: Agent, idea: str, transcript: list[dict]) -> str:
    corrections: list[str] = []
    text = ""
    for _ in range(3):
        data = agent.ask("readback", idea=idea, transcript=format_transcript(transcript),
                         corrections="\n".join(f"- {c}" for c in corrections) or "(none)", max_tokens=1500)
        text = (f"{data.get('readback', '')}\n\nIn scope: {', '.join(data.get('in_scope') or []) or '-'}\n"
                f"Out of scope: {', '.join(data.get('out_of_scope') or []) or '-'}")
        ans = agent.human.ask(HumanRequest(kind="readback", stage="plan", context=text,
                                           summary="Here is my understanding. Reply 'y' if correct, or type your corrections."))
        if ans.strip().lower() in YES:
            return text
        corrections.append(ans)
    return text + "\n\nUnresolved corrections:\n" + "\n".join(f"- {c}" for c in corrections)


# =============================================================================
# 4. WRITE_SPEC
# =============================================================================
SPEC = Prompt(
    name="spec", stage="plan", uses_tools=True,
    instructions="""Project idea:
{idea}

Interview:
{transcript}

Agreed understanding:
{understanding}

Mode guidance: {guidance}

Clarifications and feedback on previous drafts (authoritative):
{feedback}

Write the spec. Acceptance criteria must be testable statements. If a tool would clearly help later
stages (for example current facts from the web, or cost calculations), add it to tool_suggestions.""",
    output_schema="""{
  "summary": str,
  "in_scope": [str], "out_of_scope": [str],
  "assumptions": [str],            // mark each as "(stated)" or "(assumed)"
  "open_questions": [str],
  "acceptance_criteria": [{"id": "AC1", "text": str}],
  "tech_stack": {"language": str, "...": str},
  "tasks": [{"id": "T1", "title": str, "description": str, "acceptance_ids": [str],
             "domain_tags": [str], "risk_tags": [str], "files_hint": [str]}]
}""",
)


def write_spec(agent: Agent, idea: str, transcript: list[dict], understanding: str,
               estimate: Callable[[Spec], str] | None = None) -> Spec:
    quality = agent.profile.get("quality", {})
    needs_signoff = "spec" in agent.profile["human"].get("approval_required_for", [])
    feedback: list[str] = []
    critic_done = not quality.get("spec_critic", False)
    for _ in range(4):
        data = agent.ask("spec", idea=idea, transcript=format_transcript(transcript),
                         understanding=understanding or "(none)",
                         feedback="\n".join(f"- {f}" for f in feedback) or "(none)",
                         max_tokens=int(agent.profile["stages"]["plan"].get("max_output_tokens", 6000)))
        spec = Spec.from_dict(data)
        if not spec.tasks:
            feedback.append("The spec contained no tasks. Break the work into small ordered tasks.")
            continue
        if not critic_done:
            critic_done = True
            clarifications = agent.colleague("critic").do("critique_spec", spec)
            if clarifications:
                feedback += clarifications
                continue  # redraft once with the human's clarifications
        agent.store.write_json("spec.json", spec.to_dict())
        agent.store.write_text("spec.md", spec.to_markdown())
        if not needs_signoff:
            return spec
        context = spec.to_markdown()
        if estimate and quality.get("cost_estimate", True):
            context += "\n" + estimate(spec)
        approved, fb = agent.human.approve("plan", "Approve this spec? Building starts after approval.", context)
        if approved:
            return spec
        feedback.append(fb or "Rejected without comment.")
    raise FactoryStopped("Spec was not approved.")


# =============================================================================
# 5. CRITIQUE_SPEC
# =============================================================================
CRITIQUE_SPEC = Prompt(
    name="critique_spec", stage="plan",
    instructions="""Spec:
{spec}

Find at most {max_issues} concrete problems that would cause rework later: ambiguity, missing edge
cases, untestable acceptance criteria, contradictions or scope risks. Do not suggest new features.
If the spec is fine, return an empty list.""",
    output_schema='{"issues": [{"type": str, "detail": str, "question": str}]}',
)


def critique_spec(agent: Agent, spec: Spec) -> list[str]:
    """Returns the human's clarifications (empty if nothing needs changing)."""
    max_issues = int(agent.profile.get("quality", {}).get("critic_max_issues", 5))
    data = agent.ask("critique_spec", spec=spec.to_markdown(), max_issues=max_issues, max_tokens=1500)
    issues = [i for i in (data.get("issues") or []) if isinstance(i, dict)][:max_issues]
    if not issues:
        agent.telemetry.log("spec_critique", issues=0, answered=0)
        return []
    reqs = [HumanRequest(kind="spec_issue", stage="plan", summary=str(i.get("question") or i.get("detail", "")),
                         context=f"{i.get('type', 'issue')}: {i.get('detail', '')}",
                         blocking=False, default_assumption="ignore")
            for i in issues]
    clarifications = []
    for issue, ans in zip(issues, agent.human.ask_batch(reqs), strict=True):
        ans = (ans or "").strip()
        if ans and ans.lower() not in {"ignore", "skip", "-"}:
            clarifications.append(f"{issue.get('detail', '')} -> {ans}")
    agent.telemetry.log("spec_critique", issues=len(issues), answered=len(clarifications))
    return clarifications


# =============================================================================
# 6. ACCEPTANCE_TESTS
# =============================================================================
ACCEPTANCE_TESTS = Prompt(
    name="acceptance_tests", stage="tests",
    instructions="""Spec summary: {summary}
Tech stack: {tech_stack}
Mode guidance: {guidance}

Acceptance criteria:
{criteria}

Planned tasks (for context on the intended structure):
{tasks}

Write executable pytest acceptance tests, one file per criterion, at
tests/acceptance/test_<criterion id in lowercase>.py (for example tests/acceptance/test_ac1.py).
Test only observable behaviour through the public interface you expect the app to expose, and
describe that interface precisely in interface_notes (module paths, function or endpoint names,
signatures). Do not write application code. Keep each file short.""",
    output_schema='{"edits": [{"op": "write", "path": str, "content": str}], "interface_notes": str}',
)


def acceptance_tests(agent: Agent, spec: Spec) -> list[str]:
    data = agent.ask(
        "acceptance_tests", summary=spec.summary, tech_stack=spec.tech_stack or "-",
        criteria="\n".join(f"- {c['id']}: {c['text']}" for c in spec.acceptance_criteria),
        tasks="\n".join(f"- {t.id}: {t.title}" for t in spec.tasks), max_tokens=6000,
    )
    edits = [e for e in data.get("edits") or [] if str(e.get("path", "")).replace("\\", "/").startswith(ACCEPTANCE_DIR)]
    applied, errors = apply_edits(agent.store.app, edits)
    conftest = agent.store.app / "conftest.py"
    if not conftest.exists():  # makes the app root importable from tests/acceptance
        conftest.write_text('"""Makes the app root importable in tests."""\n', encoding="utf-8")
    notes = str(data.get("interface_notes", "")).strip()
    agent.store.write_text("acceptance_tests.md", (
        "# Acceptance tests\n\nWritten before implementation by the test engineer. The builder may not edit "
        f"files in `app/{ACCEPTANCE_DIR}`.\n\n## Expected interface\n{notes or '(not described)'}\n\n## Files\n"
        + "\n".join(f"- app/{p}" for p in applied) + "\n"))
    agent.telemetry.log("acceptance_tests", files=len(applied), errors=len(errors))
    return applied


def acceptance_interface(agent: Agent) -> str:
    text = agent.store.read_text("acceptance_tests.md")
    if "## Expected interface" not in text:
        return "(no acceptance tests)"
    return text.split("## Expected interface", 1)[1].split("## Files", 1)[0].strip()


# =============================================================================
# 7. BUILD
# =============================================================================
BUILD = Prompt(
    name="build", stage="build", uses_tools=True,
    instructions="""Task {task_id}: {title}
{description}

Acceptance criteria for this task:
{criteria}

Project summary: {summary}
Out of scope: {out_of_scope}
Assumptions: {assumptions}
Tech stack: {tech_stack}
Mode guidance: {guidance}

Human feedback (authoritative, overrides assumptions):
{feedback}

Interface expected by the acceptance tests (files in tests/acceptance/ are read-only for you):
{interface}

Lessons from earlier projects (hints, not rules):
{lessons}

Repository map:
{repo_map}

Relevant files:
{files}
{failure_section}""",
    output_schema="""{
  "edits": [
    {"op": "write", "path": "relative/path.py", "content": "full file content"},
    {"op": "replace", "path": "relative/path.py", "old": "exact existing text", "new": "replacement"}
  ],
  "decisions": [{"title": str, "options": [str], "chosen": str, "rationale": str,
                 "reversibility": "cheap" | "costly",
                 "category": "implementation" | "dependency" | "architecture" | "scope_change",
                 "touches_spec_scope": bool, "risk_tags": [str]}],
  "questions": [{"summary": str, "blocking": bool, "default_assumption": str | null}],
  "notes": "one or two sentences on what you did"
}""",
)


def build(agent: Agent, task: Task, spec: Spec, *, as_agent: str = "builder", tier: str | None = None,
          failure_feedback: str = "", fingerprints: list[str] | None = None, extra_files: list[str] | None = None,
          lessons: str = "") -> BuildResult:
    ctx = agent.profile.get("context", {})
    specialist = as_agent.split(":", 1)[1] if as_agent.startswith("specialist:") else None
    relevant = list(task.files_hint) + list(extra_files or []) + files_from_fingerprints(fingerprints or [])
    failure_section = ""
    if failure_feedback:
        failure_section = f"\nThe previous attempt failed. Fix the cause, do not repeat the same approach:\n{failure_feedback}"
    data = agent.ask(
        "build", tier=tier, as_agent=as_agent, system=system_prompt_for("builder", specialist),
        max_tokens=int(agent.profile["stages"]["build"].get("max_output_tokens", 8000)),
        task_id=task.id, title=task.title, description=task.description, criteria=_criteria_text(spec, task),
        summary=spec.summary, out_of_scope=", ".join(spec.out_of_scope) or "-",
        assumptions="; ".join(spec.assumptions) or "-", tech_stack=spec.tech_stack or "-",
        feedback=trim(agent.store.read_text("human_feedback.md"), int(ctx.get("max_feedback_chars", 6000))) or "(none)",
        interface=acceptance_interface(agent), lessons=lessons or "(none)",
        repo_map=repo_map(agent.store.app, int(ctx.get("repo_map_max_files", 200))),
        files=file_bundle(agent.store.app, relevant, int(ctx.get("max_file_chars", 40000))),
        failure_section=failure_section,
    )
    result = BuildResult(notes=str(data.get("notes", "")))
    result.retry_reasons += _handle_decisions(agent, data.get("decisions") or [], task)
    result.retry_reasons += _handle_questions(agent, data.get("questions") or [], task)
    if result.retry_reasons:
        return result
    protected = (ACCEPTANCE_DIR,) if agent.factory.state.acceptance_done else ()
    result.applied_files, result.edit_errors = apply_edits(agent.store.app, data.get("edits") or [], protected)
    return result


def _handle_decisions(agent: Agent, raw_decisions: list[dict], task: Task) -> list[str]:
    reasons = []
    for raw in raw_decisions:
        if not isinstance(raw, dict):
            continue
        d = Decision.from_dict(raw)
        if needs_approval(d, agent.profile):
            ok, feedback = agent.human.approve("build", f"Decision needed for {task.id}: {d.title}", d.describe())
            agent.decisions.record(d, approved=ok, by="human", task_id=task.id, feedback=feedback)
            if not ok:
                note = f"Decision '{d.title}' (proposed: {d.chosen}) was rejected. Human says: {feedback or 'no'}"
                agent.store.append_text("human_feedback.md", f"- [{task.id}] {note}\n")
                agent.knowledge.record_lesson(problem=f"Proposed {d.chosen} for: {d.title}",
                                              fix=f"Human rejected it: {feedback or 'no reason given'}",
                                              tags=[d.category, *d.risk_tags], context=task.title)
                reasons.append(note)
        else:
            agent.decisions.record(d, approved=True, by="agent", task_id=task.id)
    return reasons


def _handle_questions(agent: Agent, questions: list[dict], task: Task) -> list[str]:
    reasons = []
    for q in questions:
        summary = str(q.get("summary", "")).strip() if isinstance(q, dict) else ""
        if not summary:
            continue
        default = q.get("default_assumption")
        blocking = bool(q.get("blocking")) or not default
        answer = agent.human.ask(HumanRequest(kind="question", stage="build", summary=f"[{task.id}] {summary}",
                                              blocking=blocking, default_assumption=None if blocking else str(default)))
        if blocking:
            agent.store.append_text("human_feedback.md", f"- [{task.id}] Q: {summary} A: {answer}\n")
            reasons.append(f"Human answered a blocking question: {summary} -> {answer}")
    return reasons


# =============================================================================
# 8. VERIFY
# =============================================================================
REVIEW = Prompt(
    name="review", stage="qa",
    instructions="""Task {task_id}: {title}
{description}

Acceptance criteria:
{criteria}

Diff of the changes made for this task:
{diff}""",
    output_schema='{"pass": bool, "issues": [{"criterion": "AC id", "problem": str}]}',
)


def _acceptance_ignores(agent: Agent, allowed_criteria: set[str] | None) -> list[str]:
    """Only run acceptance tests for criteria that should already work (done tasks + current task)."""
    folder = agent.store.app / ACCEPTANCE_DIR
    if allowed_criteria is None or not folder.is_dir():
        return []
    ignores = []
    for p in sorted(folder.glob("test_*.py")):
        if p.stem[5:].upper() not in allowed_criteria:
            ignores.append(f"--ignore={ACCEPTANCE_DIR}{p.name}")
    return ignores


def _review_mode(value) -> str:
    if value is True:
        return "always"
    if value is False or value is None:
        return "never"
    return str(value)


def verify(agent: Agent, task: Task, spec: Spec, changed: list[str], snapshot: dict[str, str],
           allowed_criteria: set[str] | None = None) -> QAResult:
    cfg = agent.profile["stages"]["qa"]
    ctx = agent.profile.get("context", {})
    max_chars = int(ctx.get("max_feedback_chars", 6000))
    results = run_checks(agent.store.app, cfg.get("checks", []),
                         {"pytest": _acceptance_ignores(agent, allowed_criteria)})
    for r in results:
        agent.telemetry.log("check", task_id=task.id, name=r.name, passed=r.passed, skipped=r.skipped)
    failed = [r for r in results if not r.passed and not r.skipped]
    if failed:
        per_check = max_chars // len(failed)
        report = "\n\n".join(f"## {r.name} failed\n{trim(r.output, per_check)}" for r in failed)
        return QAResult(False, [fp for r in failed for fp in r.fingerprints], report)

    diff = diff_since(agent.store.app, snapshot, changed)
    mode = _review_mode(cfg.get("llm_review", "risky"))
    risky = bool(task.risk_tags or task.domain_tags) or diff_size(diff) >= int(cfg.get("review_min_diff_lines", 80))
    if mode == "never" or (mode == "risky" and not risky):
        agent.telemetry.log("review_skipped", task_id=task.id, mode=mode, diff_lines=diff_size(diff))
        return QAResult(True, [], "All deterministic checks passed.")

    data = agent.ask("review", task_id=task.id, title=task.title, description=task.description,
                     criteria=_criteria_text(spec, task), diff=trim(diff, int(ctx.get("max_file_chars", 40000))) or "(no diff)",
                     max_tokens=1500)
    issues = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
    if data.get("pass"):
        note = "\n".join(f"- {i.get('criterion')}: {i.get('problem')}" for i in issues)
        return QAResult(True, [], "Checks and review passed." + (f"\nNotes:\n{note}" if note else ""))
    fps = [f"review|{i.get('criterion', '?')}" for i in issues] or ["review|unspecified"]
    report = "## Review failed\n" + "\n".join(f"- {i.get('criterion')}: {i.get('problem')}" for i in issues)
    return QAResult(False, fps, trim(report, max_chars))


# =============================================================================
# 9. INTEGRATION_CHECK
# =============================================================================
def integration_check(agent: Agent) -> QAResult:
    """All checks, all acceptance tests, once at the end. Deterministic, zero tokens."""
    checks = agent.profile["stages"]["qa"].get("checks", [])
    results = run_checks(agent.store.app, checks)
    failed = [r for r in results if not r.passed and not r.skipped]
    agent.telemetry.log("integration_check", passed=not failed, failed=[r.name for r in failed])
    if not failed:
        return QAResult(True, [], "Full check suite passed.")
    per_check = int(agent.profile.get("context", {}).get("max_feedback_chars", 6000)) // len(failed)
    report = "\n\n".join(f"## {r.name} failed\n{trim(r.output, per_check)}" for r in failed)
    return QAResult(False, [fp for r in failed for fp in r.fingerprints], report)


# =============================================================================
# 10. HANDOFF
# =============================================================================
HANDOFF = Prompt(
    name="handoff", stage="handoff",
    instructions="""Write the README.md for this app, for a developer who will run and maintain it.
Cover: what it does, requirements, install, run, test, configuration, and known limitations
(from out of scope and assumptions). Use only information given here.

Spec:
{spec}

Interface notes:
{interface}

Repository map:
{repo_map}""",
    output_schema='{"readme": str}',
)


def handoff(agent: Agent, spec: Spec) -> str:
    data = agent.ask("handoff", spec=trim(spec.to_markdown(), 8000), interface=acceptance_interface(agent),
                     repo_map=repo_map(agent.store.app), max_tokens=2500)
    readme = str(data.get("readme", "")).strip()
    if readme:
        (agent.store.app / "README.md").write_text(readme + "\n", encoding="utf-8")
    agent.telemetry.log("handoff", written=bool(readme))
    return readme


# =============================================================================
# 11. DEPLOY
# =============================================================================
def deploy(agent: Agent, spec: Spec) -> DeployResult:
    cfg = agent.profile["stages"]["deploy"]
    project = agent.store.root.name
    commands = cfg.get("commands") or []
    listed = "\n".join(f"  {c}" for c in commands) or "  (none, manifest only)"
    if "deploy" in agent.profile["human"].get("approval_required_for", []):
        approved, feedback = agent.human.approve("deploy", f"Deploy '{project}'?",
                                                 f"Target: {cfg.get('target', 'local')}\nCommands:\n{listed}")
        if not approved:
            agent.telemetry.log("deploy", ok=False, skipped=True, reason=feedback)
            return DeployResult(False, True, f"Deploy not approved: {feedback}")
    ok, results = run_commands(commands, agent.store.app, project)
    rollback: list[dict] = []
    if not ok and cfg.get("rollback_commands"):
        _, rollback = run_commands(cfg["rollback_commands"], agent.store.app, project)
    agent.store.write_json("deploy_manifest.json", {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "target": cfg.get("target", "local"),
        "ok": ok, "summary": spec.summary, "results": results, "rollback": rollback,
    })
    agent.telemetry.log("deploy", ok=ok, skipped=False, target=cfg.get("target", "local"))
    return DeployResult(ok, False, "Deployed." if ok else "Deploy failed, rollback attempted.")


# =============================================================================
# 12. MONITOR
# =============================================================================
def monitor(agent: Agent) -> list[dict]:
    results = [http_health(url) for url in agent.profile["stages"]["monitor"].get("health_urls", []) or []]
    for r in results:
        agent.telemetry.log("health_check", **r)
    return results


# =============================================================================
# 13. REGISTRIES
# =============================================================================
PROMPTS: dict[str, Prompt] = {p.name: p for p in [
    CLASSIFY_MODE, INTERVIEW, READBACK, SPEC, CRITIQUE_SPEC, ACCEPTANCE_TESTS, BUILD, REVIEW, HANDOFF,
]}

TASKS: dict[str, TaskDef] = {t.name: t for t in [
    TaskDef("classify_mode", "Suggest a build mode and let the human confirm it", classify_mode, ("classify_mode",)),
    TaskDef("interview", "Ask rounds of questions until nothing blocking remains", interview, ("interview",)),
    TaskDef("readback", "Restate the understanding until the human confirms", readback, ("readback",)),
    TaskDef("write_spec", "Draft, critique, estimate and get sign-off on the spec", write_spec, ("spec",)),
    TaskDef("critique_spec", "Find rework risks in a spec and ask the human", critique_spec, ("critique_spec",)),
    TaskDef("acceptance_tests", "Write protected acceptance tests before implementation", acceptance_tests,
            ("acceptance_tests",)),
    TaskDef("build", "Implement one spec task", build, ("build",)),
    TaskDef("verify", "Deterministic checks, then diff review when warranted", verify, ("review",)),
    TaskDef("integration_check", "Run the full check suite once all tasks pass", integration_check),
    TaskDef("handoff", "Write the app README", handoff, ("handoff",)),
    TaskDef("deploy", "Approve, run deploy commands, roll back on failure", deploy),
    TaskDef("monitor", "Run health checks", monitor),
]}

