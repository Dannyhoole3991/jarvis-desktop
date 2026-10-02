"""
Read-only reporting over the registry: a dashboard of the whole research effort
(funnel, rejection reasons, risk-adjusted leaderboard, backtest vs out-of-sample
vs paper, paper status, live-readiness checklist, claim experiments, version
history) plus a short plain-text summary for Jarvis's chat. Nothing here writes
to the registry.
"""

import html
import json
import os
import time

from . import criteria as C
from . import risk as RK
from . import tracking as T

REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trading_lab", "reports")


def snapshot(registry):
    track = T.build(registry)
    versions = registry.list_versions()
    rows = []
    for v in versions:
        meta = registry.get(v["strategy_id"])
        params = registry.get_version(v["strategy_id"], v["version"])
        rows.append({**v, "source": meta["source"], "parameters": params["parameters"],
                     "training": params["training_period"], "validation": params["validation_period"],
                     "test": params["test_period"], "history": registry.status_history(v["strategy_id"], v["version"])})
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    claim = None
    path = os.path.join(REPORT_DIR, "claim_experiment.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            claim = json.load(f)
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "counts": by_status, "rows": rows, "tracking": track,
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
    for r in [r for r in snap["rows"] if r["status"] == "rejected"][:3]:
        lines.append(f"{r['strategy_id']} {r['source'].split(':')[-1]}: {r['reason']}")
    return "\n".join(lines)


def _fmt(x, pct=False, digits=2):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.1%}" if pct else f"{x:.{digits}f}"
    return html.escape(str(x))


def _metric_cell(m):
    if not m:
        return "<small>not run</small>"
    return (f"{m.get('n_trades', 0)} trades, ret {_fmt(m.get('total_return'), True)}, sharpe {_fmt(m.get('sharpe'))}, "
            f"PF {_fmt(m.get('profit_factor'))}, DD {_fmt(m.get('max_drawdown'), True)}")


def render_html(snap):
    esc = html.escape
    t = snap["tracking"]
    out = ["<!doctype html><meta charset=utf-8><title>Jarvis Trading Research Lab</title>",
           "<style>body{font:14px system-ui;margin:24px;background:#0f1419;color:#e6e6e6}h1,h2{font-weight:600}"
           "table{border-collapse:collapse;width:100%;margin:8px 0 24px}td,th{border-bottom:1px solid #2a3340;padding:6px 8px;text-align:left;vertical-align:top}"
           ".rejected{color:#ff7b72}.candidate,.paper{color:#7ee787}.backtested{color:#e3b341}small{color:#8b949e}"
           ".banner{background:#3a2a00;border:1px solid #e3b341;padding:10px;border-radius:6px}"
           ".bar{background:#2a3340;height:10px;border-radius:3px}.bar>div{background:#58a6ff;height:10px;border-radius:3px}</style>",
           "<h1>Jarvis Trading Research Lab</h1>",
           "<div class=banner>Research and paper trading only. No real money, no live trading, no leverage. "
           "A strategy that survives history is a <b>candidate</b>, not a proven edge.</div>",
           f"<p><small>Generated {esc(snap['generated'])}. Criteria fingerprint {snap['criteria_fingerprint']}, "
           f"risk limits fingerprint {snap['risk_fingerprint']}. {t['n_versions']} strategy versions, {t['n_trials']} parameter sets tried.</small></p>",
           "<h2>Funnel: how many survived each gate</h2><table><tr><th>Stage</th><th>Count</th><th></th></tr>"]
    top = max([f["count"] for f in t["funnel"]] + [1])
    for f in t["funnel"]:
        out.append(f"<tr><td>{esc(f['label'])}</td><td>{f['count']}</td><td style='width:40%'><div class=bar><div style='width:{100 * f['count'] / top:.0f}%'></div></div></td></tr>")
    out.append("</table>")

    out.append("<h2>Why strategies were rejected</h2><table><tr><th>Reason code</th><th>Strategies</th></tr>")
    for code, n in t["rejection_reasons"]:
        out.append(f"<tr><td>{esc(code)}</td><td>{n}</td></tr>")
    out.append("</table><h2>By family</h2><table><tr><th>Family</th><th>Tested</th><th>Rejected</th><th>Candidate</th><th>Paper</th><th>Best luck-adjusted score</th></tr>")
    for fam, d in sorted(t["by_family"].items()):
        out.append(f"<tr><td>{esc(fam)}</td><td>{d['tested']}</td><td>{d['rejected']}</td><td>{d['candidate']}</td><td>{d['paper']}</td><td>{_fmt(d['best_score'])}</td></tr>")
    out.append("</table>")

    out.append(f"<h2>Leaderboard (risk-adjusted, not raw profit)</h2><p><small>{esc(t['ranking_note'])}</small></p>"
               "<table><tr><th>ID</th><th>Family</th><th>Status</th><th>Score</th><th>OOS Sharpe</th><th>OOS Calmar</th><th>OOS drawdown</th>"
               "<th>Fees % of gross profit</th><th>Param instability</th><th>Sharpe vs buy-and-hold</th><th>p-value (needs &lt;)</th></tr>")
    for r in t["leaderboard"][:15]:
        o, s, c = r["out_of_sample"] or {}, r["stability"] or {}, r["confidence"] or {}
        out.append(f"<tr><td>{esc(r['strategy_id'])}</td><td>{esc(r['template'])}</td><td class={esc(r['status'])}>{esc(r['status'])}</td><td>{_fmt(r['score'])}</td>"
                   f"<td>{_fmt(o.get('sharpe'))}</td><td>{_fmt(o.get('calmar'))}</td><td>{_fmt(o.get('max_drawdown'), True)}</td>"
                   f"<td>{_fmt(o.get('fees_pct_of_gross_profit'), True)}</td><td>{_fmt(s.get('neighbour_fail_fraction'), True)}</td>"
                   f"<td>{_fmt(r.get('vs_passive_sharpe'))}</td>"
                   f"<td>{c.get('p_value') if c.get('p_value') is None else format(c['p_value'], '.2g')} ({c.get('required_p', 0):.1e})</td></tr>")
    out.append("</table>")
    if t.get("benchmark"):
        b = t["benchmark"]
        out.append(f"<p><small>Passive benchmark over the held-out window (equal-weight universe, buy-and-hold): Sharpe {_fmt(b.get('sharpe'))}, "
                   f"return {_fmt(b.get('total_return'), True)}, max drawdown {_fmt(b.get('max_drawdown'), True)}. All strategies are long-only, so "
                   "an out-of-sample Sharpe near this number is market exposure, not edge.</small></p>")

    live = [r for r in t["rows"] if r["status"] in ("candidate", "paper", "retired") or r["paper"]]
    out.append("<h2>Candidates: backtest vs out-of-sample vs paper</h2><table><tr><th>ID</th><th>Status</th><th>Development</th><th>Out-of-sample</th><th>Paper</th></tr>")
    for r in live:
        dev = r["development"] or {}
        out.append(f"<tr><td>{esc(r['strategy_id'])} {esc(r['template'])}</td><td class={esc(r['status'])}>{esc(r['status'])}</td>"
                   f"<td>{dev.get('n_trades', 0)} trades, DD {_fmt(dev.get('max_drawdown'), True)}</td><td>{_metric_cell(r['out_of_sample'])}</td>"
                   f"<td>{_metric_cell(r['paper'])}<br><small>{esc(str(r['paper_verdict'] or ''))}</small></td></tr>")
    if not live:
        out.append("<tr><td colspan=5><small>None. No strategy has survived every gate.</small></td></tr>")
    out.append("</table>")

    claim = snap.get("claim")
    if claim:
        out.append("<h2>The '3-10% per day' claim</h2><ul>")
        for key in ("summary_3pct", "summary_10pct"):
            for line in claim.get(key, [])[:8]:
                out.append(f"<li>{esc(line)}</li>")
        out.append("</ul>")

    out.append("<h2>All strategies</h2><table><tr><th>ID</th><th>Template / params</th><th>Status</th><th>Reason</th></tr>")
    for r in snap["rows"][-300:]:
        out.append(f"<tr><td>{esc(r['strategy_id'])} v{r['version']}</td><td>{esc(r['source'].split(':')[-1])}<br>"
                   f"<small>{esc(json.dumps(r['parameters'], sort_keys=True))}</small></td><td class={esc(r['status'])}>{esc(r['status'])}</td>"
                   f"<td>{esc((r['reason'] or '')[:400])}</td></tr>")
    out.append("</table><h2>Recent decisions</h2><table><tr><th>Time</th><th>Strategy</th><th>Category</th><th>Detail</th></tr>")
    for d in snap["decisions"]:
        out.append(f"<tr><td>{esc(d['ts'])}</td><td>{esc(str(d['strategy_id'] or ''))}</td><td>{esc(d['category'])}</td><td>{esc(d['detail'][:300])}</td></tr>")
    out.append("</table><h2>Risk limits (independent Risk Engine)</h2><pre>" + esc(json.dumps(snap["risk_limits"], indent=1)) + "</pre>")
    out.append("<h2>Research criteria (predefined, fingerprinted)</h2><pre>" + esc(json.dumps(snap["criteria"], indent=1)) + "</pre>")
    return "\n".join(out)


def write_report(registry):
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, "dashboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(snapshot(registry)))
    return path
