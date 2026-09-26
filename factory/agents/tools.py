"""ALL TOOLS. Everything that does work without a model lives in this file.

  1. Model tools     statistics, expenses, web_search: agents call these through "tool_calls"
  2. File tools      apply_edits, snapshot_files, diff_since
  3. Context tools   list_files, repo_map, file_bundle, trim, files_from_fingerprints
  4. Check tools     CHECKS registry, run_checks, fingerprint (deterministic QA, zero tokens)
  5. Shell and HTTP  run_commands (deploy), http_health (monitor)
  6. ToolBroker      grants model tools per agent, runs them on budget, handles suggestions

tasks.py uses sections 2 to 5 directly. Model tools (section 1) are only reached through the
ToolBroker (section 6), which checks what the agent may use, what the human allowed, and the budget."""
from __future__ import annotations

import difflib
import importlib.util
import json
import os
import re
import statistics as st
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from factory.artifacts import ArtifactStore
from factory.human import YES, HumanLoop, HumanRequest
from factory.llm import MeteredLLM
from factory.telemetry import Telemetry


# =============================================================================
# 1. MODEL TOOLS
# =============================================================================
@dataclass
class ToolResult:
    ok: bool
    output: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_schema: str
    func: Callable[[dict, dict], ToolResult]
    est_tokens_per_call: int = 0  # 0 means local and free

    @property
    def cost_label(self) -> str:
        if self.est_tokens_per_call == 0:
            return "free, runs locally"
        return f"about {self.est_tokens_per_call:,} tokens per call"


def _numbers(values) -> list[float]:
    if not isinstance(values, list) or not values:
        raise ValueError("'values' must be a non-empty list of numbers")
    return [float(v) for v in values]


def statistics_tool(args: dict, providers: dict) -> ToolResult:
    values = _numbers(args.get("values"))
    out: dict = {"count": len(values), "sum": sum(values), "mean": st.fmean(values),
                 "median": st.median(values), "min": min(values), "max": max(values)}
    if len(values) > 1:
        out["stdev"] = st.stdev(values)
        cuts = st.quantiles(values, n=100, method="inclusive")
        for p in args.get("percentiles") or [25, 75, 95]:
            if 1 <= int(p) <= 99:
                out[f"p{int(p)}"] = cuts[int(p) - 1]
    return ToolResult(True, json.dumps({k: round(v, 6) if isinstance(v, float) else v for k, v in out.items()}))


def expenses_tool(args: dict, providers: dict) -> ToolResult:
    items = args.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("'items' must be a non-empty list of {category, amount, quantity?}")
    by_category: dict[str, float] = {}
    for item in items:
        cat = str(item.get("category", "other"))
        by_category[cat] = by_category.get(cat, 0.0) + float(item.get("amount", 0)) * float(item.get("quantity", 1))
    total = sum(by_category.values())
    out: dict = {
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])},
        "share_pct": {k: round(100 * v / total, 1) if total else 0 for k, v in by_category.items()},
        "total": round(total, 2),
    }
    if args.get("vat_rate"):
        out["total_incl_vat"] = round(total * (1 + float(args["vat_rate"])), 2)
    if "budget" in args:
        budget = float(args["budget"])
        out.update(budget=budget, remaining=round(budget - total, 2), over_budget=total > budget)
    return ToolResult(True, json.dumps(out))


def web_search_tool(args: dict, providers: dict) -> ToolResult:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("'query' is required")
    provider = providers.get("web_search")
    if provider is None:
        return ToolResult(False, "No web search provider is configured for this run.")
    return provider(query)


class AnthropicWebSearch:
    """Web search through the Anthropic API server-side search tool, summarised by a cheap model."""

    def __init__(self, model: str, max_uses: int = 3, max_tokens: int = 1500):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model, self.max_uses, self.max_tokens = model, max_uses, max_tokens

    def __call__(self, query: str) -> ToolResult:
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": self.max_uses}],
            messages=[{"role": "user", "content": f"Search the web and answer concisely with source URLs: {query}"}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        return ToolResult(True, text, resp.usage.input_tokens, resp.usage.output_tokens, self.model)


class MockWebSearch:
    def __call__(self, query: str) -> ToolResult:
        return ToolResult(True, f"(mock) Top result for '{query}': example.com", 300, 100, "mock-search")


_TOOLS = [
    Tool("statistics", "Descriptive statistics for a list of numbers (mean, median, stdev, percentiles).",
         '{"values": [number], "percentiles": [int] (optional)}', statistics_tool),
    Tool("expenses", "Totals, category shares, VAT and budget check for a list of cost items.",
         '{"items": [{"category": str, "amount": number, "quantity": number}], "budget": number, "vat_rate": number}',
         expenses_tool),
    Tool("web_search", "Search the internet for current facts, docs, prices or versions. Returns a short summary with URLs.",
         '{"query": str}', web_search_tool, est_tokens_per_call=6000),
]
TOOLS: dict[str, Tool] = {t.name: t for t in _TOOLS}


# =============================================================================
# 2. FILE TOOLS
# =============================================================================
def apply_edits(repo: Path, edits: list[dict], protected: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """Apply write/replace edits inside repo. Refuses paths outside repo and protected prefixes."""
    applied, errors = [], []
    root = repo.resolve()
    protected = tuple(protected)
    for e in edits:
        rel = str(e.get("path", "")).strip().replace("\\", "/")
        target = (root / rel).resolve()
        if not rel or root not in target.parents:
            errors.append(f"Refused edit outside the repository: {rel!r}")
            continue
        if protected and rel.startswith(protected):
            errors.append(f"{rel}: protected path (acceptance tests belong to the test engineer), not edited")
            continue
        op = e.get("op")
        if op == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(e.get("content", "")), encoding="utf-8")
            applied.append(rel)
        elif op == "replace":
            if not target.exists():
                errors.append(f"{rel}: file does not exist, cannot replace")
                continue
            text = target.read_text(encoding="utf-8")
            old = str(e.get("old", ""))
            count = text.count(old) if old else 0
            if count != 1:
                errors.append(f"{rel}: 'old' text matched {count} times, expected exactly 1")
                continue
            target.write_text(text.replace(old, str(e.get("new", "")), 1), encoding="utf-8")
            applied.append(rel)
        else:
            errors.append(f"Unknown edit op {op!r} for {rel}")
    return sorted(set(applied)), errors


def snapshot_files(repo: Path, max_bytes: int = 3_000_000) -> dict[str, str]:
    """Text contents of the repo before a task, so review can look at a diff instead of whole files."""
    snap, used = {}, 0
    for p in list_files(repo):
        if p.suffix not in TEXT_SUFFIXES:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        used += len(text)
        if used > max_bytes:
            break
        snap[p.relative_to(repo).as_posix()] = text
    return snap


def diff_since(repo: Path, snapshot: dict[str, str], paths: Iterable[str]) -> str:
    chunks = []
    for rel in sorted(set(paths)):
        p = repo / rel
        new = p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else ""
        old = snapshot.get(rel, "")
        if old == new:
            continue
        chunks.extend(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                           fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
    return "".join(chunks)


def diff_size(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---")))


# =============================================================================
# 3. CONTEXT TOOLS
# =============================================================================
IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".html", ".css",
                 ".js", ".ts", ".tsx", ".jsx", ".sql", ".sh", ".env", ""}


def list_files(repo: Path) -> list[Path]:
    return [p for p in sorted(repo.rglob("*"))
            if p.is_file() and not any(part in IGNORED_DIRS for part in p.relative_to(repo).parts)]


def _summary_line(p: Path) -> str:
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip().strip('"').strip("'").strip("#").strip()
            if s and not s.startswith(("import ", "from ")):
                return s[:80]
    except OSError:
        pass
    return ""


def repo_map(repo: Path, max_files: int = 200) -> str:
    """One line per file: path, size and first meaningful line. Replaces sending the whole codebase."""
    files = list_files(repo)
    if not files:
        return "(empty repository)"
    lines = []
    for p in files[:max_files]:
        rel = p.relative_to(repo).as_posix()
        if p.suffix in TEXT_SUFFIXES:
            n = sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
            lines.append(f"{rel} ({n} lines): {_summary_line(p)}")
        else:
            lines.append(f"{rel} (binary)")
    if len(files) > max_files:
        lines.append(f"... and {len(files) - max_files} more files")
    return "\n".join(lines)


def file_bundle(repo: Path, rel_paths: Iterable[str], max_chars: int = 40000) -> str:
    parts, used = [], 0
    for rel in dict.fromkeys(rel_paths):
        p = repo / rel
        if not p.is_file():
            continue
        block = f"--- {rel} ---\n{p.read_text(encoding='utf-8', errors='ignore')}\n"
        if used + len(block) > max_chars:
            parts.append(f"--- {rel} --- (omitted, context limit reached)\n")
            continue
        parts.append(block)
        used += len(block)
    return "".join(parts) or "(no files selected)"


def trim(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n... [{len(text) - max_chars} chars trimmed] ...\n" + text[-half:]


def files_from_fingerprints(fingerprints: Iterable[str]) -> list[str]:
    paths = []
    for fp in fingerprints:
        for chunk in fp.replace("::", "|").split("|"):
            if "/" in chunk or chunk.endswith(".py"):
                paths.append(chunk)
    return paths


# =============================================================================
# 4. CHECK TOOLS (deterministic QA)
# =============================================================================
@dataclass
class CheckResult:
    name: str
    passed: bool
    output: str
    skipped: bool = False
    fingerprints: list[str] = field(default_factory=list)


def _compile_check(repo: Path) -> CheckResult:
    """Syntax check of every .py file, in-process: fastest possible failure signal, no side effects."""
    errors = []
    for p in list_files(repo):
        if p.suffix == ".py":
            rel = p.relative_to(repo).as_posix()
            try:
                compile(p.read_text(encoding="utf-8", errors="replace"), rel, "exec")
            except SyntaxError as exc:
                errors.append(f"{rel}:{exc.lineno}: SyntaxError: {exc.msg}")
    return CheckResult("compile", not errors, "\n".join(errors) or "ok",
                       fingerprints=[f"compile|{e.split(':')[0]}" for e in errors])


INTERNAL_CHECKS: dict[str, Callable[[Path], CheckResult]] = {"compile": _compile_check}

# name -> (module that must be importable, command). Commands avoid writing caches into the app.
CHECKS: dict[str, tuple[str, list[str]]] = {
    "pytest": ("pytest", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]),
    "ruff": ("ruff", [sys.executable, "-m", "ruff", "check", "--no-cache", "."]),
    "mypy": ("mypy", [sys.executable, "-m", "mypy", ".", "--ignore-missing-imports", f"--cache-dir={os.devnull}"]),
    "bandit": ("bandit", [sys.executable, "-m", "bandit", "-q", "-r", ".", "-x", "./tests"]),
}


def fingerprint(name: str, output: str, returncode: int) -> list[str]:
    fps: list[str] = []
    if name == "pytest":
        for m in re.finditer(r"^(?:FAILED|ERROR) (\S+)(?: - (\w+))?", output, re.MULTILINE):
            fps.append(f"{m.group(1)}|{m.group(2) or 'error'}")
        if returncode == 5:
            fps.append("pytest|no_tests_collected")
    elif name == "ruff":
        for m in re.finditer(r"^(\S+?):\d+:\d+: (\w+)", output, re.MULTILINE):
            fps.append(f"ruff|{m.group(2)}|{m.group(1)}")
    elif name == "mypy":
        for m in re.finditer(r"^(\S+?):\d+: error: .*\[([\w-]+)\]", output, re.MULTILINE):
            fps.append(f"mypy|{m.group(2)}|{m.group(1)}")
    return sorted(set(fps)) or [f"{name}|exit{returncode}"]


def run_checks(repo: Path, names: Iterable[str], extra_args: dict[str, list[str]] | None = None,
               timeout: int = 600) -> list[CheckResult]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    results = []
    for name in names:
        if name in INTERNAL_CHECKS:
            results.append(INTERNAL_CHECKS[name](repo))
            continue
        if name not in CHECKS:
            results.append(CheckResult(name, True, f"Unknown check '{name}', skipped.", skipped=True))
            continue
        module, cmd = CHECKS[name]
        if importlib.util.find_spec(module) is None:
            results.append(CheckResult(name, True, f"{module} not installed, skipped.", skipped=True))
            continue
        cmd = cmd + list((extra_args or {}).get(name, []))
        try:
            proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout, env=env, check=False)
        except subprocess.TimeoutExpired:
            results.append(CheckResult(name, False, f"{name} timed out", fingerprints=[f"{name}|timeout"]))
            continue
        output = (proc.stdout + "\n" + proc.stderr).strip()
        passed = proc.returncode == 0
        results.append(CheckResult(name, passed, output,
                                   fingerprints=[] if passed else fingerprint(name, output, proc.returncode)))
    return results


# =============================================================================
# 5. SHELL AND HTTP
# =============================================================================
def run_commands(commands: Iterable[str], cwd: Path, project: str, timeout: int = 1800) -> tuple[bool, list[dict]]:
    """Runs profile-defined commands only (never model output). {project} is replaced by the project name."""
    results = []
    for raw in commands:
        cmd = raw.format(project=project)
        try:
            proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
            rc, out = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            rc, out = -1, "timeout"
        results.append({"cmd": cmd, "returncode": rc, "output": trim(out, 3000)})
        if rc != 0:
            return False, results
    return True, results


def http_health(url: str, timeout: float = 5) -> dict:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status, ok = resp.status, 200 <= resp.status < 400
    except Exception as exc:  # noqa: BLE001 - any failure is an unhealthy result
        status, ok = str(exc)[:120], False
    return {"url": url, "status": status, "ok": ok, "latency_ms": round((time.perf_counter() - start) * 1000, 1)}


# =============================================================================
# 6. TOOL BROKER
# =============================================================================
TOOL_KEYS_DOC = """Optional keys you may add to your JSON:
  "tool_calls": [{"tool": name, "args": {...}}]   // results come back and you are asked again
  "tool_suggestions": [{"tool": name, "reason": str, "expected_benefit": str}]
If you add tool_calls, you may leave the other fields empty for now."""


class ToolBroker:
    """Decides which model tools an agent may call (agent allow-list AND human/profile policy),
    runs them within the token budget, and turns agents' tool suggestions into human decisions."""

    def __init__(self, profile: dict, store: ArtifactStore, human: HumanLoop, telemetry: Telemetry,
                 llm: MeteredLLM, providers: dict | None = None):
        self.store, self.human, self.telemetry, self.llm = store, human, telemetry, llm
        self.providers = providers or {}
        self.pending: dict[str, dict] = {}
        self.suggested_this_run: set[str] = set()
        self.reload(profile)

    def reload(self, profile: dict) -> None:
        cfg = profile.get("tools", {})
        self.mode = cfg.get("suggestions", "ask")  # ask | log | off
        self.max_rounds = int(cfg.get("max_rounds_per_call", 1))
        self.max_calls = int(cfg.get("max_calls_per_round", 3))
        self.max_result_chars = int(cfg.get("max_result_chars", 4000))
        policy = self.store.read_json("tool_policy.json") or {}
        self.project_enabled: set[str] = set(policy.get("enabled", []))
        self.denied: set[str] = set(cfg.get("denied", [])) | set(policy.get("denied", []))
        self.enabled: set[str] = (set(cfg.get("enabled", [])) | self.project_enabled) - self.denied

    def available(self, allowed: Iterable[str]) -> list[Tool]:
        return [TOOLS[n] for n in sorted(self.enabled & set(allowed)) if n in TOOLS]

    def prompt_section(self, allowed: Iterable[str]) -> str:
        lines = ["Tools: use them instead of your own reasoning for arithmetic, statistics, cost "
                 "calculations and current facts. Only call a tool when it clearly helps."]
        tools = self.available(allowed)
        if tools:
            lines.append("Available tools:")
            lines += [f"- {t.name} ({t.cost_label}): {t.description} Args: {t.args_schema}" for t in tools]
        else:
            lines.append("No tools are available to you right now.")
        if self.mode != "off":
            suggestible = [n for n in sorted(TOOLS) if n not in self.enabled and n not in self.denied]
            hint = f" Known but not enabled: {', '.join(suggestible)}." if suggestible else ""
            lines.append("If a missing tool would materially improve the result or save tokens, suggest it." + hint)
        lines.append(TOOL_KEYS_DOC)
        return "\n".join(lines)

    def run_calls(self, calls: list[dict], stage: str, agent: str, allowed: Iterable[str]) -> str:
        allowed = set(allowed)
        outputs = []
        for call in calls[: self.max_calls]:
            name = str(call.get("tool", "")).strip().lower()
            tool = TOOLS.get(name)
            if tool is None or name not in self.enabled or name not in allowed:
                reason = "is unknown" if tool is None else "is not available to you"
                outputs.append(f"[{name}] Tool {reason}. Continue without it; suggest it if it matters.")
                self.telemetry.log("tool_call", tool=name, stage=stage, agent=agent, ok=False, reason=reason)
                continue
            if tool.est_tokens_per_call:
                self.llm.reserve("tools", tool.est_tokens_per_call)
            try:
                result = tool.func(call.get("args") or {}, self.providers)
            except Exception as exc:  # noqa: BLE001 - tool errors go back to the agent, not up the stack
                result = ToolResult(False, f"error: {exc}")
            if result.input_tokens or result.output_tokens:
                self.llm.record_external("tools", result.input_tokens, result.output_tokens, result.model,
                                         agent=f"tool:{name}", purpose=name)
            self.telemetry.log("tool_call", tool=name, stage=stage, agent=agent, ok=result.ok,
                               tokens=result.input_tokens + result.output_tokens)
            outputs.append(f"[{name}] {trim(result.output, self.max_result_chars)}")
        return "\n".join(outputs)

    def collect_suggestions(self, suggestions, stage: str, agent: str) -> None:
        if self.mode == "off" or not suggestions:
            return
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            name = str(s.get("tool", "")).strip().lower()
            if (not name or name in self.enabled or name in self.denied
                    or name in self.pending or name in self.suggested_this_run):
                continue
            tool = TOOLS.get(name)
            entry = {"tool": name, "stage": stage, "agent": agent, "known": tool is not None,
                     "reason": str(s.get("reason", "")), "benefit": str(s.get("expected_benefit", "")),
                     "cost": tool.cost_label if tool else "unknown (not implemented yet)"}
            self.pending[name] = entry
            self.suggested_this_run.add(name)
            self.telemetry.log("tool_suggested", **entry)
            self.store.append_text("tool_suggestions.md",
                                   f"- [{stage}] {agent} suggests '{name}' ({entry['cost']}). "
                                   f"Why: {entry['reason']} Benefit: {entry['benefit']}\n")

    def review_suggestions(self) -> None:
        """Called at batch points. In 'ask' mode the human decides; otherwise suggestions are only logged."""
        if not self.pending:
            return
        entries = list(self.pending.values())
        self.pending.clear()
        if self.mode != "ask":
            return
        reqs = []
        for e in entries:
            note = "" if e["known"] else "\nNot in the tool catalog yet: it would need to be added to factory/agents/tools.py."
            reqs.append(HumanRequest(
                kind="tool_suggestion", stage=e["stage"], summary=f"The {e['agent']} agent suggests the '{e['tool']}' tool.",
                context=f"Why: {e['reason']}\nExpected benefit: {e['benefit']}\nCost: {e['cost']}{note}",
                options=["enable for this project", "not now", "never suggest again"],
            ))
        for e, ans in zip(entries, self.human.ask_batch(reqs), strict=True):
            a = (ans or "").strip().lower()
            if a.startswith("enable") or a in YES:
                decision = "enabled" if e["known"] else "noted_not_implemented"
                if e["known"]:
                    self.enabled.add(e["tool"])
                    self.project_enabled.add(e["tool"])
            elif a.startswith("never"):
                decision = "denied"
                self.denied.add(e["tool"])
                self.enabled.discard(e["tool"])
            else:
                decision = "declined"
            self.telemetry.log("tool_decision", tool=e["tool"], decision=decision)
        self.store.write_json("tool_policy.json", {"enabled": sorted(self.project_enabled - self.denied),
                                                   "denied": sorted(self.denied)})
