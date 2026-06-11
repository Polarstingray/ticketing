"""Score a resolved case: run the harness-owned acceptance test on the resolver's output
branch, and pull cost/token totals from the AgentRun records.

The acceptance test is the independent ground truth — it is written into a scratch
worktree at the resolver's branch *here* (never handed to the agent), so a case passes
only if the agent's real code change satisfies a check it never saw.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Score:
    accept_pass: bool
    accept_output: str
    n_runs: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    phases: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def run_acceptance(repo: Path, branch: str, acceptance: dict, python: str,
                   timeout: int = 300) -> tuple[bool, str]:
    """Check `branch` out into a scratch worktree, drop the acceptance files in, run the
    command, and report pass/fail. Returns (passed, combined_output)."""
    rc, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}")
    if rc != 0:
        return False, f"output branch {branch!r} does not exist"

    wt = Path(tempfile.mkdtemp(prefix="eval-score-"))
    try:
        rc, out = _git(repo, "worktree", "add", "--detach", str(wt), branch)
        if rc != 0:
            return False, f"could not create scoring worktree:\n{out}"

        # Inject the harness-owned acceptance files (overwriting anything the agent wrote
        # at those paths) so the check is ours, not the agent's.
        for rel, content in (acceptance.get("files") or {}).items():
            dst = wt / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")

        # Run the acceptance command with the chosen interpreter's bin on PATH so a bare
        # `python` / `pytest` resolves to it.
        import os
        # Prepend the interpreter's OWN bin dir (not its symlink target — a venv's
        # bin/python often links out to a system python whose dir lacks a bare `python`).
        env = dict(os.environ)
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
        try:
            proc = subprocess.run(acceptance["command"], shell=True, cwd=str(wt),
                                  capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return False, f"acceptance command timed out after {timeout}s"
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt))


def fetch_agent_runs(base_url: str, api_key: str, ticket_id: int) -> list[dict]:
    req = urllib.request.Request(
        f"{base_url}/tickets/{ticket_id}/agent-runs",
        headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return []


def score_case(repo: Path, branch: str, acceptance: dict, base_url: str, api_key: str,
               ticket_id: int, python: str, *, produced: bool,
               acceptance_timeout: int = 300) -> Score:
    """Full score for one case: acceptance (only meaningful if the case produced a
    branch) plus cost/token rollup from the AgentRuns."""
    if produced:
        ok, out = run_acceptance(repo, branch, acceptance, python, acceptance_timeout)
    else:
        ok, out = False, "case did not produce an output branch"

    runs = fetch_agent_runs(base_url, api_key, ticket_id)
    tokens = sum((r.get("input_tokens", 0) + r.get("output_tokens", 0)
                  + r.get("cache_read_tokens", 0) + r.get("cache_write_tokens", 0))
                 for r in runs)
    cost = sum(r.get("cost_usd", 0.0) for r in runs)
    phases = [f"{r.get('phase')}:{r.get('status')}" for r in runs]
    return Score(accept_pass=ok, accept_output=out, n_runs=len(runs),
                 tokens=tokens, cost_usd=cost, phases=phases)
