"""Assemble per-case results into a report: a JSON artifact, a printed table, and an
optional diff against a baseline report (so the harness can gate regressions)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def build_report(rows: list[dict], meta: dict) -> dict:
    produced = [r for r in rows if r["outcome"] == "produced"]
    passed = [r for r in rows if r["accept_pass"]]
    n = len(rows) or 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta,                              # agent, model, n_cases, …
        "summary": {
            "cases": len(rows),
            "produced": len(produced),
            "passed": len(passed),
            "pass_rate": round(len(passed) / n, 4),
            "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
            "total_tokens": sum(r["tokens"] for r in rows),
            "mean_wall_s": round(sum(r["wall_s"] for r in rows) / n, 1),
        },
        "cases": rows,
    }


def write_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"eval-{ts}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def format_table(report: dict) -> str:
    rows = report["cases"]
    w_id = max([len("case")] + [len(r["id"]) for r in rows]) if rows else 4
    head = f"{'case':<{w_id}}  {'pass':<5} {'outcome':<10} {'runs':>4} {'tokens':>9} {'cost$':>9} {'wall_s':>7}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['id']:<{w_id}}  {('✓' if r['accept_pass'] else '✗'):<5} "
            f"{r['outcome']:<10} {r['n_runs']:>4} {r['tokens']:>9} "
            f"{r['cost_usd']:>9.4f} {r['wall_s']:>7.1f}")
    s = report["summary"]
    lines += [
        "-" * len(head),
        f"pass_rate={s['pass_rate']:.0%}  passed={s['passed']}/{s['cases']}  "
        f"cost=${s['total_cost_usd']:.4f}  tokens={s['total_tokens']}  "
        f"mean_wall={s['mean_wall_s']}s",
    ]
    return "\n".join(lines)


def diff_baseline(report: dict, baseline: dict) -> tuple[str, bool]:
    """Compare per-case pass + cost against a baseline report. Returns (text, regressed)
    where `regressed` is True if any case went pass→fail or overall pass-rate dropped."""
    base_by_id = {c["id"]: c for c in baseline.get("cases", [])}
    lines = ["", "vs baseline:"]
    regressed = False
    for c in report["cases"]:
        b = base_by_id.get(c["id"])
        if not b:
            lines.append(f"  {c['id']}: NEW ({'✓' if c['accept_pass'] else '✗'})")
            continue
        if b["accept_pass"] and not c["accept_pass"]:
            lines.append(f"  {c['id']}: REGRESSED ✓→✗")
            regressed = True
        elif not b["accept_pass"] and c["accept_pass"]:
            lines.append(f"  {c['id']}: FIXED ✗→✓")
        d_cost = c["cost_usd"] - b.get("cost_usd", 0.0)
        if abs(d_cost) > 0.0001:
            lines.append(f"  {c['id']}: cost {d_cost:+.4f}$")
    rate_drop = report["summary"]["pass_rate"] < baseline.get("summary", {}).get("pass_rate", 0)
    if rate_drop:
        lines.append(f"  pass_rate {baseline['summary']['pass_rate']:.0%}"
                     f" → {report['summary']['pass_rate']:.0%}  REGRESSED")
        regressed = True
    return "\n".join(lines), regressed
