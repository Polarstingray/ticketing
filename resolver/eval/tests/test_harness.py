"""Free self-test for the eval harness (no agent, no model spend).

Exercises the whole plumbing except the live resolver run: stand up the temp backend,
seed the fixture repo, PRE-CREATE the resolver's output branch with a known-good and a
known-bad change, and assert the scorer reports pass/fail correctly and rolls up
AgentRun cost. Run with:

    .venv/bin/python -m pytest eval/tests/ --rootdir=$PWD -q -p no:cacheprovider
"""
import subprocess
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1]
RESOLVER_DIR = EVAL_DIR.parent
for p in (RESOLVER_DIR, EVAL_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from harness import backend as backend_mod   # noqa: E402
from harness import score                    # noqa: E402
from stingray import StingrayClient          # noqa: E402
import run_eval                              # noqa: E402

ACCEPTANCE = {
    "command": "python -m pytest tests/test_acceptance.py -q",
    "files": {
        "tests/test_acceptance.py": (
            "from calc import multiply\n"
            "def test_multiply():\n"
            "    assert multiply(3, 4) == 12\n"
        )
    },
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_branch(repo: Path, branch: str, calc_body: str, tmp: Path) -> None:
    """Create `branch` off main with calc.py replaced by `calc_body`, via a temp
    worktree so the main checkout is untouched."""
    _git(repo, "branch", branch)
    wt = tmp / f"build-{branch.replace('/', '-')}"
    _git(repo, "worktree", "add", "-q", str(wt), branch)
    (wt / "calc.py").write_text(calc_body, encoding="utf-8")
    _git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")
    _git(repo, "worktree", "remove", "--force", str(wt))


@pytest.fixture
def backend(tmp_path):
    be = backend_mod.start_backend(tmp_path / "stingray.db", python=sys.executable)
    yield be
    be.stop()


@pytest.fixture
def sandbox_repo(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    repo = projects / "sandbox"
    run_eval.seed_repo(EVAL_DIR / "repos" / "sandbox", repo)
    return repo


GOOD_CALC = (
    "def add(a, b):\n    return a + b\n\n"
    "def subtract(a, b):\n    return a - b\n\n"
    "def divide(a, b):\n    return a / b\n\n"
    "def multiply(a, b):\n    return a * b\n"
)
BAD_CALC = GOOD_CALC.replace("return a * b", "return a + b")  # wrong on purpose


def test_acceptance_pass_on_good_branch(sandbox_repo, tmp_path):
    _make_branch(sandbox_repo, "claude/ticket-1", GOOD_CALC, tmp_path)
    ok, out = score.run_acceptance(sandbox_repo, "claude/ticket-1", ACCEPTANCE, sys.executable)
    assert ok, out


def test_acceptance_fail_on_bad_branch(sandbox_repo, tmp_path):
    _make_branch(sandbox_repo, "claude/ticket-2", BAD_CALC, tmp_path)
    ok, _ = score.run_acceptance(sandbox_repo, "claude/ticket-2", ACCEPTANCE, sys.executable)
    assert not ok


def test_missing_branch_scores_fail(sandbox_repo):
    ok, out = score.run_acceptance(sandbox_repo, "claude/ticket-404", ACCEPTANCE, sys.executable)
    assert not ok
    assert "does not exist" in out


def test_score_case_rolls_up_agent_run_cost(backend, sandbox_repo, tmp_path):
    admin = StingrayClient(backend.base_url, backend.admin_key)
    t = admin.create_ticket(type="task", title="multiply", description="add multiply",
                            assigned_to=backend.bot_user_id, tags=["repo:sandbox"])
    tid = t["id"]
    # Record an AgentRun the way the resolver would, then a good output branch.
    admin.create_agent_run(tid, agent="claude", phase="implement", model="m",
                           input_tokens=1000, output_tokens=200, cost_usd=0.05,
                           status="succeeded")
    _make_branch(sandbox_repo, f"claude/ticket-{tid}", GOOD_CALC, tmp_path)

    sc = score.score_case(sandbox_repo, f"claude/ticket-{tid}", ACCEPTANCE,
                          backend.base_url, backend.admin_key, tid, sys.executable,
                          produced=True)
    assert sc.accept_pass
    assert sc.n_runs == 1
    assert sc.tokens == 1200
    assert sc.cost_usd == pytest.approx(0.05)
    assert sc.phases == ["implement:succeeded"]
