"""Ticket payload assembly: code blocks, repo tags, and validation.

Shared by the ``stingray`` CLI and the resolver's ``file_ticket.py``. Everything
here is argparse-free and side-effect-free apart from reading files off disk and
shelling out to ``git`` for the repo name, so both callers can validate a ticket
before anything is POSTed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TYPES = ("code_review", "task")
PRIORITIES = ("low", "medium", "high", "critical")

# Delegation / control tags (mirrors backend/control_tags.py).
TAG_DELEGATE = "delegate"
PARENT_PREFIX = "parent:"
REVIEW_BY_PREFIX = "review-by:"
REPO_PREFIX = "repo:"
REV_PREFIX = "rev:"
BRANCH_PREFIX = "branch:"

# Mirrors backend/schemas.MAX_TAG_LENGTH. A 40-char sha fits `rev:` with room to
# spare; an unusually long branch name does not, and is dropped rather than sent to
# be rejected — the sha alone still pins the review.
MAX_TAG_LENGTH = 50

# Reserved tags, mirrored from backend/control_tags.py so a client can reject an
# unsettable tag before doing expensive work (diffing a range, running an agent)
# and before the server's 422.
RESERVED_PREFIXES = ("claude:", "resolver:", "repo:", "parent:", "review-by:",
                     "rev:", "branch:")
RESERVED_EXACT = frozenset({"dangerous", "fix", "delegate"})


def is_reserved_tag(tag: str) -> bool:
    return tag in RESERVED_EXACT or tag.startswith(RESERVED_PREFIXES)


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def inherited_parent_tags(client, parent_id: int) -> list[str]:
    """Tags a delegated sub-task must inherit from its parent so its assignee can act:

    - ``review-by:<parent.created_by>`` — who the finished PR is handed back to (the
      human who asked for the audit).
    - the parent's ``repo:<name>`` — which repo to check out. The assignee can't
      discover this from the parent itself (ticket read access is restricted to a
      ticket's creator/assignee, and the worker is neither), so without this the
      worker fails with "no repo specified" and bounces the child back.
    - the parent's ``rev:``/``branch:`` — *where* in that repo. Same reasoning: an
      unpinned child silently falls back to the remote default branch, so a fan-out
      from a feature branch would have every sub-task working against main.

    We stamp these at creation because the lead bot filing the child *can* read the
    parent (it's assigned to it during the run). Best-effort: empty if the parent
    can't be read, leaving the defaults."""
    try:
        parent = client.get_ticket(parent_id)
    except Exception:
        return []
    tags: list[str] = []
    owner = parent.get("created_by")
    if owner:
        tags.append(f"{REVIEW_BY_PREFIX}{owner}")
    tags += [t for t in (parent.get("tags") or [])
             if t.startswith((REPO_PREFIX, REV_PREFIX, BRANCH_PREFIX))]
    return tags


def _git_line(root: Path, *args: str) -> str:
    """One line of `git -C root <args>` stdout, or "" on any failure."""
    try:
        out = subprocess.run(["git", "-C", str(root), *args],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def main_checkout(root: Path) -> Path | None:
    """The checkout ``root`` belongs to, resolving a linked worktree to its parent.

    Inside a worktree, ``--show-toplevel`` names the *worktree* directory, so naming a
    repo after it produces something like ``ticket-42`` — a directory that exists only
    under the resolver's work dir and resolves to nothing under PROJECTS_ROOT. The
    common git dir is shared by every worktree and lives in the main checkout, so its
    parent is the repo they all belong to.

    Returns None when ``root`` is not an ordinary checkout (not a repo at all, or a
    bare one), leaving the caller to fall back."""
    common = _git_line(root, "rev-parse", "--git-common-dir")
    if not common:
        return None
    path = Path(common)
    if not path.is_absolute():
        # git reports it relative to its own -C directory.
        path = Path(root) / path
    try:
        path = path.resolve()
    except OSError:
        return None
    # <main-checkout>/.git -> <main-checkout>. Anything else (a bare repo, an unusual
    # layout) is not a name worth guessing from.
    return path.parent if path.name == ".git" else None


def derive_repo_tag(root: Path) -> str | None:
    """`repo:<name>` for the git checkout containing `root`, or None if it isn't one.

    The resolver can't check anything out without this tag (see resolve_tickets
    .repo_name_of), and agents filing tickets routinely forget it — so we default it
    from the working tree the ticket is being filed from, the same way a delegated
    sub-task inherits its parent's repo tag.

    Worktrees are resolved to the checkout they belong to. An agent filing a ticket
    runs inside the resolver's worktree, and deriving the name from there produced
    `repo:ticket-42` — a tag that resolves to nothing, so the ticket could never be
    picked up. See main_checkout."""
    top = _git_line(root, "rev-parse", "--show-toplevel")
    if not top:
        return None
    return f"{REPO_PREFIX}{(main_checkout(root) or Path(top)).name}"


def has_repo_tag(tags: list[str]) -> bool:
    return any(t.startswith(REPO_PREFIX) for t in tags)


def parse_code_block(spec: str, root: Path) -> dict:
    """Turn a `PATH:LANGUAGE:START-END` spec into a ticket code_block, reading the
    exact lines off disk so their content never has to be escaped by hand."""
    head, _, rest = spec.rpartition(":")
    # rpartition splits on the LAST colon; one more split peels the language off,
    # leaving PATH intact even if it contained a colon (it normally won't).
    filename, _, language = head.rpartition(":")
    if not filename or not language or not rest:
        raise ValueError(
            f"--code-block {spec!r} must be PATH:LANGUAGE:START-END "
            "(e.g. backend/auth.py:python:60-66)"
        )

    start_s, _, end_s = rest.partition("-")
    try:
        start = int(start_s)
        end = int(end_s) if end_s else start
    except ValueError:
        raise ValueError(f"--code-block {spec!r}: line range must be numbers, got {rest!r}")
    if start < 1 or end < start:
        raise ValueError(f"--code-block {spec!r}: need 1 <= start <= end, got {start}-{end}")

    file_path = (root / filename)
    if not file_path.is_file():
        raise ValueError(f"--code-block {spec!r}: file not found: {file_path}")
    lines = file_path.read_text(encoding="utf-8").splitlines()
    if end > len(lines):
        raise ValueError(
            f"--code-block {spec!r}: file {filename} has {len(lines)} lines, "
            f"can't reach line {end}"
        )

    return {
        "filename": filename,
        "language": language,
        "line_start": start,
        "line_end": end,
        "content": "\n".join(lines[start - 1:end]),
    }


def build_payload(
    *,
    type: str,
    title: str,
    description: str = "",
    priority: str = "medium",
    tags: list[str] | None = None,
    code_block_specs: list[str] | None = None,
    code_blocks: list[dict] | None = None,
    root: Path | str = ".",
    repo: str | None = None,
    no_repo: bool = False,
    rev: str | None = None,
    branch: str | None = None,
    parent: int | None = None,
    assign: int | None = None,
    warn=_warn,
) -> dict:
    """Validate the pieces of a ticket and assemble the POST body.

    Keyword-only and argparse-free so both the CLI and the resolver's
    ``file_ticket.py`` (which keeps a thin Namespace adapter) can share it.
    ``code_block_specs`` are ``PATH:LANG:START-END`` strings read off disk;
    ``code_blocks`` are already-built dicts (what ``stingray review`` produces
    from a diff). ``rev``/``branch`` pin the ticket to the commit it was filed
    against. Raises ValueError on bad input.
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("--title must not be empty")

    specs = code_block_specs or []
    blocks = list(code_blocks or [])
    if (specs or blocks) and type != "code_review":
        raise ValueError("--code-block is only valid with --type code_review")

    root = Path(root).resolve()
    tags = list(tags or [])

    if parent is not None:
        # A delegated sub-task. The `parent:<id>` link makes it self-driving: the
        # worker that picks it up plans it and lets its review AI auto-approve the
        # plan (falling back to dangerous, no-plan implement when no review AI is
        # configured) — see resolve_tickets.do_plan/process. We deliberately do NOT
        # force `dangerous` here anymore: the old behavior implemented children with
        # no plan and no review at all. Keep it a LEAF: a child may never carry
        # `delegate`, so it can't fan out further (one level only).
        if TAG_DELEGATE in tags:
            raise ValueError(
                "a delegated sub-task (--parent) may not be tagged 'delegate' — "
                "fan-out is one level only"
            )
        tags.append(f"{PARENT_PREFIX}{parent}")

    # Target repo. Explicit --repo wins; otherwise default it from the git checkout
    # we're filing from, so the tag stops going missing. --no-repo opts out (e.g. a
    # review of pasted code blocks with no checkout to point at). A sub-task inherits
    # its parent's repo in main(), so don't guess one here.
    repo = (repo or "").strip()
    if repo and has_repo_tag(tags):
        raise ValueError("--repo conflicts with a repo: tag passed via --tag; use one")
    if repo:
        tags.append(f"{REPO_PREFIX}{repo}")
    elif not no_repo and not has_repo_tag(tags) and parent is None:
        derived = derive_repo_tag(root)
        if derived:
            tags.append(derived)
            warn(f"auto-tagged {derived} (from the git checkout at {root}; "
                 f"pass --repo NAME or --no-repo to override)")

    # Where in the repo. Only meaningful alongside a repo tag — without a checkout to
    # resolve them against, a sha and a branch name point at nothing. A sub-task
    # inherits its parent's pin (see inherited_parent_tags), so don't add one here.
    if has_repo_tag(tags) and parent is None:
        rev = (rev or "").strip()
        branch = (branch or "").strip()
        if rev and not any(t.startswith(REV_PREFIX) for t in tags):
            tags.append(f"{REV_PREFIX}{rev}")
        if branch and not any(t.startswith(BRANCH_PREFIX) for t in tags):
            tag = f"{BRANCH_PREFIX}{branch}"
            if len(tag) <= MAX_TAG_LENGTH:
                tags.append(tag)
            else:
                warn(f"branch name {branch!r} is too long to tag "
                     f"(>{MAX_TAG_LENGTH} chars with the 'branch:' prefix); pinning "
                     f"the commit only, so a fix will target the default branch")

    payload: dict = {
        "type": type,
        "title": title,
        "description": description or "",
        "priority": priority,
        "tags": tags,
        "code_blocks": [parse_code_block(s, root) for s in specs] + blocks,
    }
    if assign is not None:
        payload["assigned_to"] = assign
    return payload
