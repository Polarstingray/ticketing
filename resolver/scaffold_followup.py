"""The `/scaffold` command's follow-up: turn a stubbed worktree into a backlog.

The `/scaffold` standard command has the agent stub a feature into an existing
repo and write an `ASSIGNMENT.md` handout. This module does the two things that
follow, deterministically, so they don't depend on the agent getting them right:

- **The handout** is lifted out of the worktree before the commit and posted as a
  ticket comment. It is coursework, not code: `git add -A` honours `.gitignore`,
  so a handout the agent correctly ignored would otherwise never reach anyone.
- **The backlog** is scanned out of the finished tree — one ticket per
  `STINGRAY-STUB` marker in the files the run actually touched — rather than
  scraped out of the agent's log the way delegation does. Scraping is fine for a
  handful of sub-tasks; for ten-plus stubs an exact scan is the only honest way to
  guarantee every marker got a ticket and no marker got two.

The scaffold ticket itself is the epic: children carry `epic:<its id>` and never
the reserved `parent:<id>`, which would make each one self-driving — exactly wrong
for a backlog a learner is supposed to work through by hand.
"""
from __future__ import annotations

from pathlib import Path

from stingray_client.stubs import (
    MAX_STUB_TICKETS,
    Stub,
    build_stub_payload,
    epic_tag,
    filed_checklist,
)

COMMAND_NAME = "scaffold"
ASSIGNMENT_FILE = "ASSIGNMENT.md"

SCAFFOLD_MARKER = "📐 **Scaffolded**"


def is_scaffold(command) -> bool:
    """Whether this run was invoked by the `/scaffold` standard command."""
    return command is not None and getattr(command, "name", None) == COMMAND_NAME


def already_scaffolded(comments: list[dict], bot_id: int) -> bool:
    """Whether this ticket already has its backlog, so a re-run doesn't double it.

    Keyed off the bot's own marker comment, the same way `already_reviewed` works.
    A re-run after a `/revise` legitimately rewrites the skeleton, but it must not
    file a second copy of every stub ticket.
    """
    return any(c.get("user_id") == bot_id and SCAFFOLD_MARKER in (c.get("body") or "")
               for c in comments)


def take_assignment(wt: Path) -> str:
    """Read the handout out of the worktree and remove it. "" if there is none.

    Called before the commit. Removing it is belt-and-braces: the agent is told to
    gitignore it, but an agent that forgot would otherwise commit the answer key
    for its own exercise into the learner's branch.
    """
    path = wt / ASSIGNMENT_FILE
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    path.unlink(missing_ok=True)
    return text


def touched_files(run, wt: Path, base_ref: str) -> set[str]:
    """Repo-relative paths this run changed, for restricting the stub scan.

    Without this, stubbing one module into a repo that already uses the convention
    elsewhere would re-file a ticket for every pre-existing marker in it.
    """
    rc, out = run(["git", "-C", str(wt), "diff", "--name-only", f"{base_ref}..HEAD"])
    if rc != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def file_stub_tickets(client, ticket: dict, repo: Path, wt: Path,
                      stubs: list[Stub], *, priority: str = "medium",
                      warn=None) -> list[tuple[int, str]]:
    """File one ticket per stub, linked to this ticket as their epic.

    ``repo=repo.name`` is passed explicitly on purpose: ``derive_repo_tag`` shells
    ``git rev-parse --show-toplevel``, which inside a worktree resolves to
    ``ticket-<id>`` — a repo the resolver could never check out.
    """
    filed: list[tuple[int, str]] = []
    for stub in stubs:
        payload = build_stub_payload(
            wt, repo.name, stub,
            epic_id=ticket["id"],
            priority=priority,
            repo=repo.name,
            warn=warn,
        )
        try:
            child = client.create_ticket(**payload)
        except Exception as exc:  # a single bad child must not lose the rest
            if warn:
                warn(f"could not file a ticket for {stub.path}:{stub.line}: {exc}")
            continue
        filed.append((child["id"], child["title"]))
    return filed


def rollup(ticket: dict, assignment: str, stubs: list[Stub],
           filed: list[tuple[int, str]], truncated: int = 0) -> str:
    """The comment posted on the scaffold ticket: the handout plus the backlog."""
    lines = [
        f"{SCAFFOLD_MARKER} — the skeleton is on the branch below, and every "
        f"`STINGRAY-STUB` in it now has its own ticket. Work them in the order the "
        f"milestones give; nothing here is implemented for you.",
        "",
    ]
    if filed:
        lines += [f"**{len(filed)} exercise ticket(s)** (tagged `{epic_tag(ticket['id'])}`)",
                  "", filed_checklist(filed), ""]
    else:
        lines += ["_No stub tickets were filed — the run left no `STINGRAY-STUB` "
                  "markers in the files it changed._", ""]
    if truncated:
        lines += [f"⚠️ {truncated} further stub(s) were left un-ticketed: this run hit the "
                  f"{MAX_STUB_TICKETS}-ticket cap. Split the exercise into smaller "
                  "pieces if you want them all tracked.", ""]
    if assignment:
        lines += ["---", "", "## The assignment", "",
                  "_(kept out of the commit on purpose — this comment is the copy that "
                  "reaches you)_", "", assignment, ""]
    return "\n".join(lines)


def pr_note(filed: list[tuple[int, str]]) -> str:
    """The line appended to the PR/implementation summary, linking the backlog."""
    if not filed:
        return ""
    ids = ", ".join(f"#{tid}" for tid, _ in filed)
    return (f"\n\n---\n\nThis is a **skeleton**, not an implementation. "
            f"{len(filed)} exercise ticket(s) were filed against it: {ids}.")
