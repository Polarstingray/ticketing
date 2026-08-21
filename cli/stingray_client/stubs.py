"""The stub convention: scanning ``STINGRAY-STUB:`` markers and shaping their tickets.

The convention is two lines, so it is both machine-scannable and fatal at runtime
if you forget to implement it:

    def charge_card(token: str, cents: int) -> str:
        \"\"\"Charge `token` for `cents`; returns the provider charge id.\"\"\"
        # STINGRAY-STUB: implement against the payment provider.
        # ACCEPTANCE: idempotent per token; raises on a declined card.
        raise NotImplementedError("STINGRAY-STUB")

Only the ``STINGRAY-STUB:`` comment triggers a ticket, via a comment-syntax
agnostic regex, so a template in any language works.

This lives in the shared client package (not ``stingray_cli``) because two front
doors produce guided projects from the same convention: ``stingray scaffold``
renders a template into an empty repo, and the resolver's ``/scaffold`` standard
command stubs a feature into a repo that already has code. Both scan the tree and
file the same shaped backlog, so the scanner and the payload builder are shared
and argparse-free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from stingray_client.languages import language_for
from stingray_client.tickets import build_payload

STUB_MARKER = "STINGRAY-STUB"
_STUB_RE = re.compile(r"(?://|#|\*|--|;)\s*STINGRAY-STUB:\s*(.+?)\s*$")
_ACCEPTANCE_RE = re.compile(r"(?://|#|\*|--|;)\s*ACCEPTANCE:\s*(.+?)\s*$")

# Where a stub's enclosing definition starts, per language family.
_DEF_RE = re.compile(
    r"^\s*(?:(?:async\s+)?def\s+|class\s+|(?:export\s+)?(?:async\s+)?function\s+"
    r"|func\s+|fn\s+|(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+\w+\s*\()"
)

MAX_BLOCK_LINES = 120

# Bound how many tickets one scaffold can file, so an enthusiastic AI pass can't
# dump 200 tickets into the tracker. Shared by both front doors.
MAX_STUB_TICKETS = 30

# Only source files are scanned for stubs. Prose files are excluded on purpose:
# a project's own CLAUDE.md documents the convention and therefore *contains* the
# marker, which would otherwise file a ticket against the documentation. The same
# exclusion covers ASSIGNMENT.md, which quotes the convention back at the learner.
_CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java", ".kt",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".php", ".swift", ".scala",
    ".sh", ".bash", ".sql", ".css", ".scss", ".html", ".vue", ".svelte",
}

# Directories never worth scanning: vendored, generated, or virtualenvs. The
# resolver runs this against a real repo rather than a freshly rendered template,
# so a stray `node_modules` is a live risk there in a way it never was for the CLI.
_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", "vendor", "target",
})

# A comment line, for stitching a wrapped STINGRAY-STUB/ACCEPTANCE note back
# together. Templates wrap at 88 columns like any other code.
_COMMENT_RE = re.compile(r"^\s*(?://|#|\*|--|;)\s?(.*)$")


@dataclass
class Stub:
    path: str          # repo-relative
    line: int          # 1-indexed line of the marker
    summary: str       # text after "STINGRAY-STUB:"
    acceptance: str = ""
    block_start: int = 0
    block_end: int = 0


def _enclosing_block(lines: list[str], marker_idx: int) -> tuple[int, int]:
    """1-indexed span of the definition containing ``lines[marker_idx]``.

    Walks up to the nearest definition line, then down to the first line at or
    below that definition's indentation. Falls back to a window around the marker
    when nothing recognizable is found, so an unusual language still gets a block.
    """
    start_idx = None
    for i in range(marker_idx, -1, -1):
        if _DEF_RE.match(lines[i]):
            start_idx = i
            break
    if start_idx is None:
        lo = max(0, marker_idx - 10)
        hi = min(len(lines) - 1, marker_idx + 10)
        return lo + 1, hi + 1

    indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())
    end_idx = len(lines) - 1
    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            end_idx = i - 1
            break
    end_idx = min(end_idx, start_idx + MAX_BLOCK_LINES - 1)
    return start_idx + 1, end_idx + 1


def _continued(lines: list[str], start: int, first: str) -> tuple[str, int]:
    """Join a note with the comment lines that continue it.

    Returns the joined text and the index just past it. A continuation ends at
    the first non-comment line, or at the next STINGRAY-STUB:/ACCEPTANCE: marker.
    """
    parts = [first]
    idx = start
    while idx < len(lines):
        if _STUB_RE.search(lines[idx]) or _ACCEPTANCE_RE.search(lines[idx]):
            break
        match = _COMMENT_RE.match(lines[idx])
        if not match or not match.group(1).strip():
            break
        parts.append(match.group(1).strip())
        idx += 1
    return " ".join(parts), idx


def scan_stubs(root: Path, *, only: set[str] | None = None) -> list[Stub]:
    """Every ``STINGRAY-STUB:`` marker under ``root``, with its enclosing block.

    ``only`` restricts the scan to a set of repo-relative paths. The resolver
    passes the files its run actually touched, so stubbing one module into a big
    repo doesn't re-file tickets for every pre-existing marker elsewhere in it.
    """
    stubs: list[Stub] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _SKIP_DIRS.intersection(rel.parts):
            continue
        if path.suffix.lower() not in _CODE_SUFFIXES:
            continue
        if only is not None and str(rel) not in only:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for idx, line in enumerate(lines):
            match = _STUB_RE.search(line)
            if not match:
                continue
            summary, next_idx = _continued(lines, idx + 1, match.group(1))

            acceptance = ""
            for offset in range(next_idx, min(next_idx + 2, len(lines))):
                found = _ACCEPTANCE_RE.search(lines[offset])
                if found:
                    acceptance, _ = _continued(lines, offset + 1, found.group(1))
                    break

            start, end = _enclosing_block(lines, idx)
            stubs.append(Stub(
                path=str(rel),
                line=idx + 1,
                summary=summary,
                acceptance=acceptance,
                block_start=start,
                block_end=end,
            ))
    return stubs


def stub_code_block(root: Path, stub: Stub) -> dict | None:
    """The stub's enclosing definition as a ticket ``code_block``, read off disk."""
    try:
        lines = (root / stub.path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    start = max(1, stub.block_start)
    end = min(stub.block_end, len(lines))
    if end < start:
        return None
    return {
        "filename": stub.path,
        "language": language_for(stub.path),
        "line_start": start,
        "line_end": end,
        "content": "\n".join(lines[start - 1:end]),
    }


def stub_checklist(stubs: list[Stub]) -> str:
    """The markdown checklist of stubs that goes in an epic's description."""
    return "\n".join(f"- [ ] `{s.path}:{s.line}` — {s.summary}" for s in stubs)


def filed_checklist(filed: list[tuple[int, str]]) -> str:
    """The markdown checklist of filed child tickets."""
    return "\n".join(f"- [ ] #{tid} {title}" for tid, title in filed)


def epic_tag(epic_id: int) -> str:
    """The tag linking a stub ticket to its epic.

    ``epic:<id>`` is a FREE tag on purpose. The reserved ``parent:<id>`` would make
    each child self-driving (the resolver auto-approves a child's plan and goes
    straight to implement) — wrong for a backlog meant to be filled in by hand.

    Note the server's tag filter is a substring match, so ``epic:4`` also matches
    ``epic:42``; filter client-side when querying.
    """
    return f"epic:{epic_id}"


def build_stub_payload(
    root: Path,
    project: str,
    stub: Stub,
    *,
    epic_id: int,
    priority: str = "medium",
    assign: int | None = None,
    repo: str | None = None,
    body: str | None = None,
    extra_tags: list[str] | None = None,
    warn=None,
) -> dict:
    """The POST body for one stub's ticket.

    ``body`` is optional agent-written exercise prose; without it the description
    falls back to the marker's own summary and ACCEPTANCE line. ``repo`` must be
    passed explicitly by any caller running inside a git worktree —
    ``derive_repo_tag`` would otherwise yield the worktree's basename
    (``ticket-<id>``) rather than the repo's.
    """
    block = stub_code_block(root, stub)
    description = [
        f"Implement the `{STUB_MARKER}` at `{stub.path}:{stub.line}`.",
        "",
        body.strip() if body and body.strip() else stub.summary,
    ]
    if stub.acceptance:
        description += ["", f"**Acceptance:** {stub.acceptance}"]
    description += ["", f"Part of epic #{epic_id}."]

    kwargs = {}
    if warn is not None:
        kwargs["warn"] = warn
    return build_payload(
        type="code_review" if block else "task",
        title=f"{project}: {stub.summary}"[:200],
        description="\n".join(description),
        priority=priority,
        tags=["scaffold", "stub", epic_tag(epic_id)] + list(extra_tags or []),
        code_blocks=[block] if block else [],
        root=root,
        repo=repo,
        assign=assign,
        **kwargs,
    )
