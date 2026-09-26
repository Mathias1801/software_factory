"""Static HTML dashboard: factory health (cost, loops, escalations, human input) and product health."""
from __future__ import annotations

from collections import Counter, defaultdict
from html import escape
from pathlib import Path

from factory.artifacts import ArtifactStore
from factory.budget import TokenLedger
from factory.telemetry import Telemetry

CSS = """
:root{--bg:#fafaf8;--card:#fff;--text:#1f1f1d;--muted:#6b6a65;--line:#e4e2da;--bar:#534AB7;--ok:#0F6E56;--bad:#A32D2D;--warn:#854F0B}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1a;--card:#262624;--text:#eceae3;--muted:#a3a19a;--line:#3a3a37;--bar:#AFA9EC;--ok:#5DCAA5;--bad:#F09595;--warn:#EF9F27}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.5}
main{max-width:1100px;margin:0 auto;padding:32px 20px}h1{font-size:22px;font-weight:500;margin:0 0 4px}
h2{font-size:16px;font-weight:500;margin:32px 0 12px}.muted{color:var(--muted);font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi .v{font-size:24px;font-weight:500}.kpi .l{font-size:12px;color:var(--muted)}
.wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:14px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line)}th{font-weight:500;color:var(--muted);font-size:12px}
.bar{height:8px;background:var(--line);border-radius:4px;min-width:120px}.bar span{display:block;height:100%;background:var(--bar);border-radius:4px}
.bar.over span{background:var(--bad)}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
"""


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p class="muted">No data yet.</p>'
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _kpi(value: str, label: str) -> str:
    return f'<div class="kpi"><div class="v">{escape(value)}</div><div class="l">{escape(label)}</div></div>'


def build_dashboard(store: ArtifactStore, telemetry: Telemetry, ledger: TokenLedger) -> Path:
    events = telemetry.read()
    calls = [e for e in events if e["event"] == "llm_call"]
    task_results: dict[str, dict] = {}
    for e in events:
        if e["event"] == "task_result":
            task_results[e["task_id"]] = e
    human = [e for e in events if e["event"] == "human_request"]
    overruled = [e for e in events if e["event"] == "assumption_overruled"]
    assumptions = [e for e in events if e["event"] == "assumption_made"]
    escalations = [e for e in events if e["event"] == "escalation"]
    health = [e for e in events if e["event"] == "health_check"]
    specialist_calls = [c for c in calls if str(c.get("agent", "")).startswith("specialist:")]
    tool_calls = [e for e in events if e["event"] == "tool_call"]
    tool_suggested = [e for e in events if e["event"] == "tool_suggested"]
    tool_decisions = {e["tool"]: e["decision"] for e in events if e["event"] == "tool_decision"}

    passed = [t for t in task_results.values() if t.get("outcome") == "passed"]
    first_pass = [t for t in passed if t.get("first_pass")]
    fp_rate = f"{100 * len(first_pass) / len(task_results):.0f}%" if task_results else "-"
    healthy = [h for h in health if h.get("ok")]
    health_rate = f"{100 * len(healthy) / len(health):.0f}%" if health else "-"

    kpis = "".join([
        _kpi(f"{ledger.total_used():,}", "Tokens used"),
        _kpi(f"{len(passed)}/{len(task_results)}", "Tasks passed"),
        _kpi(fp_rate, "First-pass QA rate"),
        _kpi(str(len(specialist_calls)), "Specialist calls"),
        _kpi(str(len(tool_calls)), "Tool calls"),
        _kpi(str(len(human)), "Human requests"),
        _kpi(f"{len(overruled)}/{len(assumptions)}", "Assumptions overruled"),
        _kpi(health_rate, "Health checks OK"),
    ])

    stage_rows = []
    for stage, v in ledger.report().items():
        used, budget = v["used"], v["budget"]
        pct = 100 * used / budget if budget else 0
        cls = "bar over" if budget and used > budget else "bar"
        stage_rows.append([
            escape(stage), f"{used:,}", f"{budget:,}" if budget else "-",
            f'<div class="{cls}"><span style="width:{min(pct, 100):.0f}%"></span></div>',
        ])

    by_agent: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
    for c in calls:
        key = (c.get("agent", "?"), c.get("tier", "?"), c.get("model", "?"))
        by_agent[key][0] += 1
        by_agent[key][1] += int(c.get("input_tokens", 0))
        by_agent[key][2] += int(c.get("output_tokens", 0))
    agent_rows = [[escape(a), escape(t), escape(m), str(n), f"{i:,}", f"{o:,}"] for (a, t, m), (n, i, o) in sorted(by_agent.items())]

    task_rows = []
    for t in task_results.values():
        cls = "ok" if t.get("outcome") == "passed" else "bad"
        task_rows.append([
            escape(t["task_id"]), f'<span class="{cls}">{escape(str(t.get("outcome")))}</span>',
            str(t.get("attempts")), "yes" if t.get("first_pass") else "no", escape(str(t.get("final_agent"))),
            escape(str(t.get("final_tier"))), f"{int(t.get('tokens_spent', 0)):,}",
        ])

    human_counts = Counter((h.get("stage"), h.get("kind")) for h in human)
    human_rows = [[escape(str(s)), escape(str(k)), str(n)] for (s, k), n in sorted(human_counts.items())]

    esc_rows = [[escape(str(e.get("task_id"))), str(e.get("attempt")), escape(str(e.get("agent"))),
                 escape(str(e.get("tier"))), str(e.get("failure_count"))] for e in escalations]

    tool_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for t in tool_calls:
        row = tool_stats[t.get("tool", "?")]
        row[0] += 1
        row[1] += 1 if t.get("ok") else 0
        row[2] += int(t.get("tokens", 0) or 0)
    tool_rows = [[escape(n), str(c), str(ok), f"{tok:,}"] for n, (c, ok, tok) in sorted(tool_stats.items())]
    suggestion_rows = [[escape(str(s.get("tool"))), escape(str(s.get("agent"))), escape(str(s.get("reason"))),
                        escape(str(s.get("cost"))), escape(tool_decisions.get(s.get("tool"), "pending"))]
                       for s in tool_suggested]

    latest_health: dict[str, dict] = {}
    for h in health:
        latest_health[h["url"]] = h
    health_rows = [[escape(u), f'<span class="{"ok" if h.get("ok") else "bad"}">{escape(str(h.get("status")))}</span>',
                    f"{h.get('latency_ms')} ms", escape(h.get("ts", ""))] for u, h in latest_health.items()]

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Factory dashboard: {escape(store.root.name)}</title>
<style>{CSS}</style></head><body><main>
<h1>Factory dashboard: {escape(store.root.name)}</h1>
<p class="muted">Built from factory_state/telemetry.jsonl. Rebuild with: python -m factory dashboard --project {escape(store.root.name)}</p>
<div class="kpis">{kpis}</div>
<h2>Tokens by stage</h2>{_table(["Stage", "Used", "Budget", ""], stage_rows)}
<h2>Tokens by agent and model</h2>{_table(["Agent", "Tier", "Model", "Calls", "Input", "Output"], agent_rows)}
<h2>Tasks</h2>{_table(["Task", "Outcome", "Attempts", "First pass", "Final agent", "Final tier", "Tokens"], task_rows)}
<h2>Escalations</h2>{_table(["Task", "Attempt", "Agent", "Tier", "Repeat count"], esc_rows)}
<h2>Tool usage</h2>{_table(["Tool", "Calls", "Succeeded", "Tokens"], tool_rows)}
<h2>Tool suggestions</h2>{_table(["Tool", "Suggested by", "Why", "Cost", "Decision"], suggestion_rows)}
<h2>Human interaction</h2>{_table(["Stage", "Kind", "Count"], human_rows)}
<h2>Product health</h2>{_table(["URL", "Status", "Latency", "Checked"], health_rows)}
</main></body></html>"""
    return store.write_text("dashboard.html", html)
