"""Feature discovery for ``stingray explore``: repo in, one ticket per feature out.

The split mirrors ``describe.py``: everything here is pure logic — enumerate the
tracked files, build the prompt that asks a local agent to carve the repo into
features, parse what comes back, and turn each feature's file list into ticket
code blocks. The argparse surface and the POSTing live in ``cmd_explore.py``.

Unlike ``describe``, a failure here is *not* best-effort recoverable: there is no
deterministic fallback for "what are this codebase's features", so a bad agent
response means the command fails rather than files something invented.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from stingray_cli import gitctx
from stingray_client.languages import language_for
from stingray_client.tickets import PRIORITIES

# Enough of a file tree for a model to see the shape of a repo. Past this the
# listing is mostly test fixtures and the prompt stops paying for itself.
MAX_FILES = 500
# Files quoted per feature ticket. A reading guide wants the entry point and the
# core logic, not every file the feature touches.
MAX_FILES_PER_FEATURE = 3

# Feature discovery reads the repo, so it is a strictly bigger job than describing
# one diff. See describe.DEFAULT_TIMEOUT for why these are generous.
DEFAULT_TIMEOUT = 900

_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


class ExploreError(Exception):
    """The agent ran but produced no usable feature list."""


def list_repo_files(root: Path) -> list[str]:
    """Tracked, reviewable paths in ``root`` — generated and binary files dropped."""
    out = gitctx.git(root, "ls-files")
    return [
        path for path in (line.strip() for line in out.splitlines())
        if path and not gitctx._matches(path, gitctx.DEFAULT_EXCLUDES)
    ]


def build_discovery_prompt(root: Path, files: list[str], feature_filter: str | None,
                           teach: bool) -> str:
    """Ask the agent to carve the repo into features and describe each one."""
    shown = files[:MAX_FILES]
    parts = [
        f"You are mapping the repository '{root.name}' into its significant FEATURES, "
        "so that each one can become a code-review ticket. This is a reading guide: "
        "do not change any code.",
        "",
        "A feature is a cluster of files that together implement one user-visible "
        "capability or one significant internal subsystem. Judge by what the code "
        "does, not by directory layout — a feature routinely spans a router, a "
        "service, a model and a component.",
        "",
        f"Tracked files ({len(shown)} shown of {len(files)}):",
        *(f"  {p}" for p in shown),
    ]
    if len(files) > len(shown):
        parts.append(f"  … and {len(files) - len(shown)} more")

    parts += ["", "Read the files that matter before describing them. Do not infer a "
              "feature's behaviour from its filename."]

    if feature_filter:
        parts += [
            "",
            f"Scope: cover ONLY the feature called '{feature_filter}'. Return exactly "
            "one entry for it. If nothing in this repo matches that name, return an "
            "empty list rather than the nearest thing you can find.",
        ]
    else:
        parts += [
            "",
            "Scope: cover the whole codebase, 3 to 10 features. Skip lock files, "
            "generated assets, vendored dependencies, configuration and pure "
            "scaffolding — those are context, not features.",
        ]

    if teach:
        parts += [
            "",
            "TEACH MODE. The reader is a student learning this codebase, not a peer "
            "who already knows it. Write each description as a mentor explaining the "
            "system. Each one should cover:",
            "- what the feature does, in plain language, before any code detail;",
            "- the *why* behind the design — the constraint or failure mode that led "
            "here, and what the obvious-but-wrong alternative would have been;",
            "- how it connects to the rest of the system: what calls in, what it "
            "calls, where its data comes from and goes;",
            "- patterns worth studying and generalizing beyond this repo;",
            "- the non-obvious details: the line that looks redundant but isn't, the "
            "ordering that matters, the edge case being defended against;",
            "- two or three questions the student should be able to answer afterwards.",
        ]
    else:
        parts += [
            "",
            "Keep each description factual and reviewer-oriented: what the feature is, "
            "which files implement it, and what a reviewer should scrutinize.",
        ]

    parts += [
        "",
        "Reply with a single fenced ```json block containing a list, and nothing else:",
        '[{"name": "auth", "title": "Session auth and API keys",',
        '  "description": "markdown …", "priority": "medium",',
        '  "files": ["backend/auth.py", "backend/routers/auth.py"]}]',
        "",
        "Rules:",
        "- name: a short lowercase slug for the feature.",
        "- title: <= 100 chars, no 'Review:' prefix (it is added later).",
        "- description: markdown, grounded in code you actually read.",
        f"- files: real paths from the list above, most representative first (the "
        f"first {MAX_FILES_PER_FEATURE} are quoted into the ticket).",
        "- priority: how central the feature is to understanding the system, not risk.",
        "- Do not invent files or features you have not seen.",
    ]
    return "\n".join(parts)


def parse_feature_tickets(text: str) -> list[dict]:
    """Pull the feature list out of the agent's output, strictly then leniently.

    Returns [] rather than raising: the caller decides whether an empty result is
    a hard error (it is) so it can say which repo and agent produced it.
    """
    raw = _find_json_list(text)
    if raw is None:
        return []

    features: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        title = str(entry.get("title") or "").strip()
        files = [str(f).strip() for f in (entry.get("files") or []) if str(f).strip()]
        if not name or not title or not files:
            continue
        if name in seen:
            continue
        seen.add(name)

        priority = str(entry.get("priority") or "").strip().lower()
        if priority not in PRIORITIES:
            priority = ""

        features.append({
            "name": name,
            "title": title[:120],
            "description": str(entry.get("description") or "").strip()[:8000],
            "priority": priority,
            "files": files,
        })
    return features


def _find_json_list(text: str) -> list | None:
    """Whole output, then the last ```json fence, then the outermost brackets."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    for candidate in reversed(_FENCE_RE.findall(text)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue

    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def build_code_blocks_for_feature(root: Path, files: list[str], rev: str | None, *,
                                  max_files: int = MAX_FILES_PER_FEATURE,
                                  max_block_lines: int = 400) -> list[dict]:
    """Quote the head of each representative file as a ticket code block.

    Content comes from ``rev`` when we have one, so the blocks match the commit the
    ticket pins itself to rather than a worktree that has since drifted — the same
    rule ``gitctx`` follows for a committed range.
    """
    blocks: list[dict] = []
    for path in files[:max_files]:
        lines = gitctx._file_lines(root, path, rev)
        if not lines:
            # Hallucinated path, deleted file, or binary: skip it rather than file a
            # ticket quoting nothing.
            continue
        end = min(len(lines), max_block_lines)
        blocks.append({
            "filename": path,
            "language": language_for(path),
            "line_start": 1,
            "line_end": end,
            "content": "\n".join(lines[:end]),
        })
    return blocks
