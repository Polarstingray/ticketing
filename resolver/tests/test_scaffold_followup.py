"""The `/scaffold` command and its post-implement follow-up.

The follow-up is the part that must be exactly right: it decides which markers
become tickets, and a mistake either drops a learner's exercise on the floor or
files it twice. No network, no agent runs.
"""
from pathlib import Path

import commands
import scaffold_followup as sf
from conftest import BOT, FakeClient

PY_WITH_STUBS = '''\
def alpha(x):
    """Do alpha."""
    # STINGRAY-STUB: implement alpha over x.
    # ACCEPTANCE: returns 0 for an empty x.
    raise NotImplementedError("STINGRAY-STUB")


def beta(y):
    """Do beta."""
    # STINGRAY-STUB: implement beta.
    raise NotImplementedError("STINGRAY-STUB")
'''

TICKET = {"id": 42, "title": "Payments exercise", "priority": "medium"}


# --- the command file itself -------------------------------------------------

def test_scaffold_command_is_loadable_and_is_a_task():
    """`task` routes plan -> approve -> implement, so a human sees the skeleton
    design before any code exists. `code_review` would make it read-only."""
    c = commands.load_command("scaffold")
    assert c is not None
    assert c.type == "task"
    assert c.name == sf.COMMAND_NAME


def test_scaffold_command_forbids_implementing_and_self_filing():
    body = commands.load_command("scaffold").body
    assert "STINGRAY-STUB" in body
    assert "Do not implement the feature." in body
    assert "Do not file tickets" in body, \
        "the resolver files them; an agent that also files them doubles the backlog"
    assert "ASSIGNMENT.md" in body and ".gitignore" in body


def test_scaffold_is_detected_from_a_ticket_description():
    ticket = {"description": "/scaffold add a payments module", "title": "x"}
    command, unknown = commands.detect_command(ticket, [], BOT)
    assert unknown is None
    assert sf.is_scaffold(command)


def test_other_commands_do_not_trigger_the_follow_up():
    assert not sf.is_scaffold(commands.load_command("security-audit"))
    assert not sf.is_scaffold(None)


# --- the handout -------------------------------------------------------------

def test_the_handout_is_lifted_out_of_the_worktree(tmp_path: Path):
    """It must not reach the commit — it is the brief, not the code."""
    (tmp_path / "ASSIGNMENT.md").write_text("# Exercise\n\nBuild it.\n", encoding="utf-8")
    text = sf.take_assignment(tmp_path)
    assert "Build it." in text
    assert not (tmp_path / "ASSIGNMENT.md").exists()


def test_a_missing_handout_is_not_an_error(tmp_path: Path):
    assert sf.take_assignment(tmp_path) == ""


# --- which files get scanned -------------------------------------------------

def test_touched_files_reads_the_range_diff():
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return 0, "src/pay.py\nsrc/models.py\n"

    assert sf.touched_files(fake_run, Path("/wt"), "abc123") == {
        "src/pay.py", "src/models.py"}
    assert "--name-only" in calls[0] and "abc123..HEAD" in calls[0]


def test_touched_files_is_empty_when_git_fails():
    """An empty set means "scan nothing", which is the safe direction: better to
    file no tickets than to file one per pre-existing marker in the whole repo."""
    assert sf.touched_files(lambda argv, **kw: (128, "fatal:"), Path("/wt"), "x") == set()


# --- filing the backlog ------------------------------------------------------

def _stubs(tmp_path: Path):
    from stingray_client.stubs import scan_stubs
    (tmp_path / "pay.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    return scan_stubs(tmp_path)


def test_children_are_filed_under_this_ticket_as_the_epic(tmp_path: Path):
    client = FakeClient()
    stubs = _stubs(tmp_path)
    filed = sf.file_stub_tickets(client, TICKET, Path("/repos/shop"), tmp_path, stubs)

    assert len(filed) == 2
    for payload in client.created:
        assert "epic:42" in payload["tags"]
        assert "repo:shop" in payload["tags"], \
            "the repo tag must be the repo, never the worktree basename"
        assert not any(t.startswith("parent:") for t in payload["tags"]), \
            "parent: makes a child self-driving — a learner's exercise must not be"


def test_one_failed_child_does_not_lose_the_others(tmp_path: Path):
    class Flaky(FakeClient):
        def create_ticket(self, **fields):
            if "alpha" in fields["title"]:
                raise RuntimeError("boom")
            return super().create_ticket(**fields)

    warnings = []
    filed = sf.file_stub_tickets(Flaky(), TICKET, Path("/repos/shop"), tmp_path,
                                 _stubs(tmp_path), warn=warnings.append)
    assert len(filed) == 1
    assert warnings and "boom" in warnings[0]


# --- idempotency -------------------------------------------------------------

def test_a_rerun_does_not_refile_the_backlog():
    prior = [{"user_id": BOT, "body": f"{sf.SCAFFOLD_MARKER} — 3 tickets"}]
    assert sf.already_scaffolded(prior, BOT)


def test_someone_elses_comment_does_not_count_as_scaffolded():
    """Only the bot's own marker is trustworthy; tags and quotes are forgeable."""
    prior = [{"user_id": 99, "body": f"nice, {sf.SCAFFOLD_MARKER} looks good"}]
    assert not sf.already_scaffolded(prior, BOT)
    assert not sf.already_scaffolded([], BOT)


# --- the roll-up comment -----------------------------------------------------

def test_rollup_carries_the_handout_and_the_checklist(tmp_path: Path):
    body = sf.rollup(TICKET, "# Exercise\n\nBuild it.", _stubs(tmp_path),
                     [(101, "shop: alpha"), (102, "shop: beta")])
    assert sf.SCAFFOLD_MARKER in body
    assert "- [ ] #101 shop: alpha" in body
    assert "Build it." in body
    assert "epic:42" in body


def test_rollup_says_so_when_nothing_was_stubbed():
    body = sf.rollup(TICKET, "", [], [])
    assert "No stub tickets were filed" in body


def test_rollup_flags_the_ticket_cap(tmp_path: Path):
    body = sf.rollup(TICKET, "", _stubs(tmp_path), [(1, "a")], truncated=7)
    assert "7 further stub(s)" in body


def test_pr_note_marks_the_branch_as_a_skeleton():
    note = sf.pr_note([(101, "a"), (102, "b")])
    assert "skeleton" in note and "#101" in note
    assert sf.pr_note([]) == ""


# --- the wiring, against a real git tree -------------------------------------

def _worktree(tmp_path: Path, subprocess_run) -> tuple[Path, str]:
    """A repo with one commit of prior code, then a commit adding stubbed code."""
    wt = tmp_path / "wt"
    wt.mkdir()

    def git(*a):
        return subprocess_run(["git", "-C", str(wt), *a],
                              capture_output=True, text=True, check=True)

    git("init", "-q")
    git("config", "user.email", "t@e.com")
    git("config", "user.name", "T")
    # Pre-existing code that already uses the convention — it must NOT be ticketed.
    (wt / "legacy.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "before")
    base = git("rev-parse", "HEAD").stdout.strip()
    (wt / "pay.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "scaffold")
    return wt, base


def test_followup_tickets_only_what_the_run_touched(tmp_path, fake_cfg, monkeypatch):
    import subprocess

    import resolve_tickets as rt

    wt, base = _worktree(tmp_path, subprocess.run)
    client = FakeClient()
    note = rt.do_scaffold_followup(fake_cfg, client, dict(TICKET),
                                   Path("/repos/shop"), wt, base, "# Exercise\n\nGo.")

    files = {b["filename"] for p in client.created for b in p["code_blocks"]}
    assert files == {"pay.py"}, "legacy.py predates the run and is already someone's work"
    assert len(client.created) == 2
    assert "skeleton" in note

    body = client.comments_added[0][1]
    assert sf.SCAFFOLD_MARKER in body and "Go." in body


def test_followup_is_idempotent_across_reruns(tmp_path, fake_cfg):
    import subprocess

    import resolve_tickets as rt

    wt, base = _worktree(tmp_path, subprocess.run)
    prior = [{"user_id": BOT, "body": f"{sf.SCAFFOLD_MARKER} — filed #101, #102"}]
    client = FakeClient(comments=prior)
    note = rt.do_scaffold_followup(fake_cfg, client, dict(TICKET),
                                   Path("/repos/shop"), wt, base, "# Exercise")

    assert client.created == [], "a rework must not file a second copy of the backlog"
    assert "earlier run" in note
