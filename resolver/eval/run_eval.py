#!/usr/bin/env python
"""Resolver evaluation harness — orchestrator.

Runs the real, unmodified resolver against a set of golden tickets in a throwaway
sandbox and scores each by an independent acceptance test. Measures pass-rate + cost so
prompt/model/routing changes can be compared instead of guessed at.

    # run all cases against the Claude resolver
    .venv/bin/python eval/run_eval.py --agent claude --model claude-opus-4-8

    # one case, keep the sandbox to inspect
    .venv/bin/python eval/run_eval.py --case add-multiply --keep

    # compare against a saved baseline (non-zero exit if anything regressed)
    .venv/bin/python eval/run_eval.py --baseline eval/reports/eval-<ts>.json

Self-contained: stands up a temp Stingray backend (uvicorn + temp SQLite), seeds a bot +
admin, copies the fixture repos into a temp PROJECTS_ROOT, drives each ticket through
plan→approve→implement, then scores the output branch.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
RESOLVER_DIR = EVAL_DIR.parent
for p in (RESOLVER_DIR, EVAL_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from harness import backend as backend_mod    # noqa: E402
from harness import driver, report, score     # noqa: E402
from stingray import StingrayClient           # noqa: E402


def load_cases(cases_dir: Path, only: list[str] | None) -> list[dict]:
    cases = []
    for path in sorted(cases_dir.glob("*.yaml")):
        case = yaml.safe_load(path.read_text(encoding="utf-8"))
        case.setdefault("priority", "medium")
        case.setdefault("type", "task")
        case.setdefault("tags", [])
        case.setdefault("max_steps", 4)
        if only and case["id"] not in only:
            continue
        cases.append(case)
    if only:
        found = {c["id"] for c in cases}
        for want in only:
            if want not in found:
                raise SystemExit(f"no case with id {want!r} in {cases_dir}")
    return cases


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def seed_repo(src: Path, dst: Path) -> None:
    """Copy a fixture repo into the sandbox and make it a git repo with one commit and a
    local bare `origin`.

    The origin matters: with no remote, the resolver's resolve_base() falls back to the
    symbolic ref "HEAD", so its post-implement ahead-count becomes `HEAD..HEAD` (always 0)
    and a correct change is misreported as "no code changes". Real targets always have an
    origin (origin/main is a stable base), so we mirror that. The origin is a plain local
    bare repo — `gh pr create` against it fails fast and the resolver falls back to its
    "pushed to a local branch" path, which is exactly what we score off.
    """
    if dst.exists():
        return
    shutil.copytree(src, dst)
    _git(dst, "init", "-q", "-b", "main")
    _git(dst, "-c", "user.name=eval", "-c", "user.email=eval@eval.local", "add", "-A")
    _git(dst, "-c", "user.name=eval", "-c", "user.email=eval@eval.local",
         "commit", "-q", "-m", "fixture baseline")
    bare = dst.parent / f"{dst.name}.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(dst), str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _git(dst, "remote", "add", "origin", str(bare))
    _git(dst, "fetch", "-q", "origin")          # populates refs/remotes/origin/main
    _git(dst, "remote", "set-head", "origin", "main")


def write_env_file(path: Path, *, base_url: str, bot_key: str, bot_id: int,
                   projects_root: Path, agent: str, model: str) -> None:
    lines = [
        f"STINGRAY_URL={base_url}",          # direct to uvicorn — no /api proxy here
        f"STINGRAY_API_KEY={bot_key}",
        f"RESOLVER_BOT_USER_ID={bot_id}",
        f"PROJECTS_ROOT={projects_root}",
        f"RESOLVER_AGENT={agent}",
        f"AGENT_MODEL={model}",
        "PATCH_FALLBACK=0",
        "VERIFY_COMMAND=",                   # harness scores independently
        "MAX_ATTEMPTS=2",
    ]
    # Pass the plan-critique gate through when configured in the harness env, so
    # `CRITIQUE_API_URL=… eval/run_eval.py --baseline …` A/Bs the gate vs. baseline.
    for key in ("CRITIQUE_API_URL", "CRITIQUE_API_KEY", "CRITIQUE_API_MODEL",
                "CRITIQUE_MAX_REVISIONS",
                # Difficulty routing: forward the implement tiers + escalation gate so
                # `AGENT_IMPLEMENT_MODEL_EASY=… eval/run_eval.py …` A/Bs routing.
                "AGENT_IMPLEMENT_MODEL_EASY", "AGENT_IMPLEMENT_MODEL_HARD",
                "ESCALATE_TO_USER_ID"):
        val = os.environ.get(key)
        if val:
            lines.append(f"{key}={val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolver evaluation harness")
    ap.add_argument("--cases", type=Path, default=EVAL_DIR / "cases")
    ap.add_argument("--repos", type=Path, default=EVAL_DIR / "repos")
    ap.add_argument("--case", action="append", help="run only this case id (repeatable)")
    ap.add_argument("--agent", default="claude")
    ap.add_argument("--model", default="")
    ap.add_argument("--out", type=Path, default=EVAL_DIR / "reports")
    ap.add_argument("--baseline", type=Path, help="report JSON to diff against")
    ap.add_argument("--max-steps", type=int, default=4)
    ap.add_argument("--keep", action="store_true", help="keep the sandbox for inspection")
    args = ap.parse_args()

    python = sys.executable
    cases = load_cases(args.cases, args.case)
    if not cases:
        raise SystemExit("no cases to run")

    sandbox = Path(tempfile.mkdtemp(prefix="resolver-eval-"))
    projects = sandbox / "projects"
    projects.mkdir()
    print(f"[eval] sandbox: {sandbox}")

    be = backend_mod.start_backend(sandbox / "stingray.db", python=python)
    print(f"[eval] backend up at {be.base_url} (bot user {be.bot_user_id})")
    admin = StingrayClient(be.base_url, be.admin_key)

    try:
        # Seed fixture repos + one ticket per case (assigned to the bot).
        id_map: dict[str, int] = {}
        for case in cases:
            seed_repo(args.repos / case["repo"], projects / case["repo"])
            tags = list(case.get("tags", [])) + [f"repo:{case['repo']}"]
            t = admin.create_ticket(
                type=case["type"], title=case["title"],
                description=case.get("description", ""), priority=case["priority"],
                assigned_to=be.bot_user_id, tags=tags,
                code_blocks=case.get("code_blocks", []))
            id_map[case["id"]] = t["id"]
            print(f"[eval] seeded #{t['id']} <- {case['id']}")

        env_file = sandbox / "resolver.env"
        write_env_file(env_file, base_url=be.base_url, bot_key=be.bot_key,
                       bot_id=be.bot_user_id, projects_root=projects,
                       agent=args.agent, model=args.model)

        rows = []
        for case in cases:
            tid = id_map[case["id"]]
            print(f"[eval] driving #{tid} ({case['id']}) …")
            dr = driver.drive_case(admin, be.bot_user_id, tid, env_file,
                                   python=python, max_steps=case.get("max_steps", args.max_steps))
            sc = score.score_case(projects / case["repo"], dr.branch, case["acceptance"],
                                  be.base_url, be.admin_key, tid, python,
                                  produced=(dr.outcome == "produced"))
            rows.append({
                "id": case["id"], "ticket_id": tid, "outcome": dr.outcome,
                "accept_pass": sc.accept_pass, "n_runs": sc.n_runs,
                "tokens": sc.tokens, "cost_usd": round(sc.cost_usd, 6),
                "phases": sc.phases, "wall_s": round(dr.wall_s, 1),
                "branch": dr.branch,
                "accept_output": sc.accept_output[-800:],
            })
            print(f"[eval]   {case['id']}: {dr.outcome}, "
                  f"accept={'PASS' if sc.accept_pass else 'FAIL'}, ${sc.cost_usd:.4f}")

        meta = {"agent": args.agent, "model": args.model, "cases": len(rows)}
        rep = report.build_report(rows, meta)
        out_path = report.write_report(rep, args.out)
        print("\n" + report.format_table(rep))
        print(f"\n[eval] report: {out_path}")

        regressed = False
        if args.baseline:
            import json
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            text, regressed = report.diff_baseline(rep, baseline)
            print(text)
        return 1 if regressed else 0
    finally:
        be.stop()
        if args.keep:
            print(f"[eval] kept sandbox: {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
