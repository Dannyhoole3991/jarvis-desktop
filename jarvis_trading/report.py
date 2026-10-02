"""
Read-only reporting over the registry: a dashboard of every strategy version
(active, candidate, rejected), why each was rejected, results, version history,
recent decisions, risk limits, and the claim-experiment findings. Also a short
plain-text summary for Jarvis's chat. Nothing here writes to the registry.
"""

import html
import json
import os
import time

from . import criteria as C
from . import risk as RK

REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trading_lab", "reports")


def snapshot(registry):
    versions = registry.list_versions()
    rows = []
    for v in versions:
        exps = {}
        for e in registry.experiments(v["strategy_id"], v["version"]):
            exps[e["kind"]] = e["metrics"]  # last of each kind
        meta = registry.get(v["strategy_id"])
        params = registry.get_version(v["strategy_id"], v["version"])
        rows.append({**v, "source": meta["source"], "parameters": params["parameters"],
                     "training": params["training_period"], "validation": params["validation_period"],
                     "test": params["test_period"], "experiments": exps,
                     "history": registry.status_history(v["strategy_id"], v["version"])})
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    claim = None
    path = os.path.join(REPORT_DIR, "claim_experiment.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            claim = json.load(f)
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "counts": by_status, "rows": rows,
            "decisions": registry.recent_decisions(40), "claim": claim,
            "risk_limits": RK.RiskLimits().__dict__, "risk_fingerprint": RK.RiskLimits().fingerprint(),
            "criteria": C.ResearchCriteria().__dict__, "criteria_fingerprint": C.ResearchCriteria().fingerprint()}


def chat_summary(registry):
    snap = snapshot(registry)
    counts = snap["counts"]
    total = sum(counts.values())
    if not total:
        return "The trading research lab has no strategies yet. It is research and paper trading only; no real money is involved."
    lines = [f"Trading research lab (research and paper only, no real money): {total} strategy versions tested - "
             + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) + "."]
    cands = [r for r in snap["rows"] if r["status"] in ("candidate", "paper")]
    if cands:
        lines.append("Surviving: " + "; ".join(f"{r['strategy_id']} ({r['source'].split(':')[-1]}, {r['status']})" for r in cands))
    else:
        lines.append("No strategy has survived the research gates, so nothing is being paper traded and no edge has been found.")
    for r in [r for r in snap["rows"] if r["status"] == "rejected"][:4]:
        lines.append(f"{r['strategy_id']} {r['source'].split(':')[-1]}: {r['reason']}")
    return "\n".join(lines)


def _fmt(x, pct=False):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.1%}" if pct else f"{x:.2f}"
    return html.escape(str(x))


def render_html(snap):
    esc = html.escape
    out = ["<!doctype html><meta charset=utf-8><title>Jarvis Trading Research Lab</title>",
           "<style>body{font:14px system-ui;margin:24px;background:#0f1419;color:#e6e6e6}h1,h2{font-weight:600}"
           "table{border-collapse:collapse;width:100%;margin:8px 0 24px}td,th{border-bottom:1px solid #2a3340;padding:6px 8px;text-align:left;vertical-align:top}"
           ".rejected{color:#ff7b72}.candidate,.paper{color:#7ee787}.backtested{color:#e3b341}small{color:#8b949e}"
           ".banner{background:#3a2a00;border:1px solid #e3b341;padding:10px;border-radius:6px}</style>",
           "<h1>Jarvis Trading Research Lab</h1>",
           "<div class=banner>Research and paper trading only. No real money, no live trading, no leverage. "
           "A strategy that survives history is a <b>candidate</b>, not a proven edge.</div>",
           f"<p><small>Generated {esc(snap['generated'])}. Criteria fingerprint {snap['criteria_fingerprint']}, "
           f"risk limits fingerprint {snap['risk_fingerprint']}.</small></p>",
           "<h2>Summary</h2><p>" + ", ".join(f"{n} {esc(s)}" for s, n in sorted(snap["counts"].items())) + "</p>",
           "<h2>Strategies</h2><table><tr><th>ID</th><th>Template / params</th><th>Status</th><th>In-sample</th><th>Out-of-sample</th><th>Reason</th></tr>"]
    for r in snap["rows"]:
        ex = r["experiments"]
        tr, va, te = ex.get("backtest_training"), ex.get("backtest_validation"), ex.get("backtest_test")

        def cell(m):
            if not m:
                return "<small>not run</small>"
            return (f"{m.get('n_trades', 0)} trades, ret {_fmt(m.get('total_return'), True)}, sharpe {_fmt(m.get('sharpe'))}, "
                    f"PF {_fmt(m.get('profit_factor'))}, DD {_fmt(m.get('max_drawdown'), True)}")
        out.append(f"<tr><td>{esc(r['strategy_id'])} v{r['version']}</td>"
                   f"<td>{esc(r['source'].split(':')[-1])}<br><small>{esc(json.dumps(r['parameters'], sort_keys=True))}</small></td>"
                   f"<td class={esc(r['status'])}>{esc(r['status'])}</td>"
                   f"<td>train: {cell(tr)}<br>valid: {cell(va)}</td><td>{cell(te)}</td><td>{esc(r['reason'] or '')}</td></tr>")
    out.append("</table>")
    claim = snap.get("claim")
    if claim:
        out.append("<h2>The '3-10% per day' claim</h2><ul>")
        for key in ("summary_3pct", "summary_10pct"):
            for line in claim.get(key, [])[:8]:
                out.append(f"<li>{esc(line)}</li>")
        out.append("</ul>")
        out.append("<p><small>At 3%/day, 1000 becomes about "
                   f"{claim['compounding']['0.03']['after_1_year']:,.0f} in a year; the claim implies that.</small></p>")
    out.append("<h2>Recent decisions</h2><table><tr><th>Time</th><th>Strategy</th><th>Category</th><th>Detail</th></tr>")
    for d in snap["decisions"]:
        out.append(f"<tr><td>{esc(d['ts'])}</td><td>{esc(str(d['strategy_id'] or ''))}</td><td>{esc(d['category'])}</td><td>{esc(d['detail'])}</td></tr>")
    out.append("</table><h2>Risk limits (independent Risk Engine)</h2><pre>" + esc(json.dumps(snap["risk_limits"], indent=1)) + "</pre>")
    out.append("<h2>Research criteria (predefined, fingerprinted)</h2><pre>" + esc(json.dumps(snap["criteria"], indent=1)) + "</pre>")
    return "\n".join(out)


def write_report(registry):
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, "dashboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(snapshot(registry)))
    return path
