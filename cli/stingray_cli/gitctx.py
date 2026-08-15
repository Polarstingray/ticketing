"""Git plumbing: resolve a change set, and turn its diff into ticket code blocks.

The central correctness rule here is *where the block content comes from*:

- For working-tree changes, read the file off disk.
- For a committed range, read ``git show <rev>:<path>``.

Reading disk for a historical range pairs that commit's line numbers with a
drifted worktree, which is silently wrong in exactly the way a reviewer would
never notice.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# git's canonical empty tree, used so a repo's first commit is still diffable.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# Extension → the language label stored on a code block (drives UI highlighting).
_LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".swift": "swift", ".scala": "scala",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish",
    ".sql": "sql", ".css": "css", ".scss": "scss", ".html": "html",
    ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".md": "markdown", ".rst": "rst", ".xml": "xml",
}

# Paths never worth reviewing: generated, vendored, or binary-ish.
DEFAULT_EXCLUDES = (
    "*.lock", "package-lock.json", "yarn.lock", "poetry.lock", "Cargo.lock",
    "*.min.js", "*.min.css", "*.map",
    "dist/*", "build/*", "node_modules/*", "*/node_modules/*",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.svg",
    "*.pdf", "*.zip", "*.gz", "*.woff", "*.woff2", "*.ttf",
    "*.webm", "*.mp4", "*.mov",
)


class GitError(Exception):
    """A git invocation failed in a way the user needs to see."""


def git(root: Path | str, *args: str, check: bool = True) -> str:
    """Run a git command in ``root`` and return its stdout."""
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path | str = ".") -> Path:
    """The top level of the checkout containing ``start``."""
    try:
        out = git(start, "rev-parse", "--show-toplevel")
    except GitError as exc:
        raise GitError(f"not a git repository: {start}") from exc
    return Path(out.strip())


def _rev_exists(root: Path, rev: str) -> bool:
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", rev],
                          capture_output=True, text=True)
    return proc.returncode == 0


def default_branch(root: Path) -> str:
    """Best guess at the integration branch, for branch-style ranges."""
    for candidate in ("main", "master"):
        if _rev_exists(root, candidate):
            return candidate
    return "HEAD"


@dataclass
class ChangeSet:
    """What is being reviewed, and how to read its content."""
    root: Path
    # The committed diff range, e.g. "HEAD~1..HEAD". Empty means no commits.
    range: str = ""
    # The rev whose content the committed blocks reflect (the range's right side).
    rev: str = ""
    # Whether uncommitted working-tree changes are folded in.
    worktree: bool = False
    # True when the worktree is the *only* source (e.g. --staged).
    worktree_only: bool = False
    staged_only: bool = False
    description: str = ""
    commits: list[str] = field(default_factory=list)


def resolve_range(root: Path, spec: str | None, *, staged: bool = False,
                  include_worktree: bool | None = None) -> ChangeSet:
    """Work out what to review.

    Default (no ``spec``): the last commit plus anything uncommitted — "the work
    I just did". An explicit ``spec`` means exactly that range, and does *not*
    quietly fold in the worktree unless asked, so `stingray review abc123` is
    reproducible.
    """
    if staged:
        return ChangeSet(root=root, worktree=True, worktree_only=True, staged_only=True,
                         description="staged changes")

    has_commits = _rev_exists(root, "HEAD")

    if spec:
        rng, rev = _normalize_spec(root, spec)
        return ChangeSet(
            root=root, range=rng, rev=rev,
            worktree=bool(include_worktree),
            commits=_commit_subjects(root, rng),
            description=rng + (" + working tree" if include_worktree else ""),
        )

    if not has_commits:
        # A fresh repo: everything is uncommitted.
        return ChangeSet(root=root, worktree=True, worktree_only=True,
                         description="working tree (no commits yet)")

    if _rev_exists(root, "HEAD~1"):
        rng = "HEAD~1..HEAD"
    else:
        # Root commit: diff against the empty tree so the first commit is reviewable.
        rng = f"{EMPTY_TREE}..HEAD"

    want_worktree = True if include_worktree is None else include_worktree
    dirty = want_worktree and bool(git(root, "status", "--porcelain").strip())
    return ChangeSet(
        root=root, range=rng, rev="HEAD", worktree=dirty,
        commits=_commit_subjects(root, rng),
        description="last commit" + (" + working tree" if dirty else ""),
    )


def _normalize_spec(root: Path, spec: str) -> tuple[str, str]:
    """Turn a user-supplied range into ``(range, right-hand rev)``.

    Accepts ``A..B`` / ``A...B`` as-is, a branch name as ``merge-base(main, X)..X``,
    and a bare commit as ``REV~1..REV``.
    """
    if ".." in spec:
        right = spec.split("..")[-1] or "HEAD"
        return spec, right

    if not _rev_exists(root, spec):
        raise GitError(f"unknown revision: {spec}")

    # A branch (not a bare SHA) reads as "everything on this branch".
    is_branch = subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{spec}"],
    ).returncode == 0
    if is_branch:
        base = default_branch(root)
        if base != spec:
            merge_base = git(root, "merge-base", base, spec, check=False).strip()
            if merge_base:
                return f"{merge_base}..{spec}", spec
    if _rev_exists(root, f"{spec}~1"):
        return f"{spec}~1..{spec}", spec
    return f"{EMPTY_TREE}..{spec}", spec


def _commit_subjects(root: Path, rng: str) -> list[str]:
    out = git(root, "log", "--format=%h %s", rng, check=False)
    return [line for line in out.splitlines() if line.strip()]


def _matches(path: str, patterns) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(path, pat) or fnmatch(Path(path).name, pat) for pat in patterns)


@dataclass
class Hunk:
    path: str
    start: int
    end: int


def _parse_diff(diff: str) -> list[Hunk]:
    """Post-image line ranges of every hunk in a unified diff.

    Binary files carry no hunks, so they drop out for free. A pure deletion has a
    post-image length of 0 and is skipped: there is nothing left to quote.
    """
    hunks: list[Hunk] = []
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+++ /dev/null"):
            path = ""  # file deleted; its hunks have no post-image
        elif line.startswith("@@") and path:
            m = _HUNK_RE.match(line)
            if not m:
                continue
            start = int(m.group(1))
            length = int(m.group(2)) if m.group(2) is not None else 1
            if length == 0:
                continue
            hunks.append(Hunk(path=path, start=start, end=start + length - 1))
    return hunks


def _merge(hunks: list[Hunk], gap: int = 10) -> list[Hunk]:
    """Coalesce hunks in the same file separated by less than ``gap`` lines, so a
    function edited in three places becomes one readable block."""
    merged: list[Hunk] = []
    for hunk in hunks:
        if merged and merged[-1].path == hunk.path and hunk.start - merged[-1].end <= gap:
            merged[-1].end = max(merged[-1].end, hunk.end)
        else:
            merged.append(Hunk(hunk.path, hunk.start, hunk.end))
    return merged


def _file_lines(root: Path, path: str, rev: str | None) -> list[str] | None:
    """Content of ``path`` at ``rev``, or from the worktree when ``rev`` is None."""
    if rev:
        out = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{path}"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            return None
        return out.stdout.splitlines()
    target = root / path
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return None


def language_for(path: str) -> str:
    return _LANGUAGES.get(Path(path).suffix.lower(), "text")


@dataclass
class BlockResult:
    blocks: list[dict]
    skipped: list[str]
    truncated: bool = False


def collect_blocks(
    change: ChangeSet,
    *,
    context: int = 3,
    max_blocks: int = 40,
    max_block_lines: int = 400,
    max_total_lines: int = 4000,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    includes: tuple[str, ...] = (),
) -> BlockResult:
    """Build ticket ``code_blocks`` covering everything ``change`` touches."""
    root = change.root
    sources: list[tuple[list[Hunk], str | None]] = []

    if change.range and not change.worktree_only:
        diff = git(root, "diff", f"-U{context}", "--no-color", "--no-ext-diff",
                   "-M", change.range)
        # Content must come from the commit, not disk — see the module docstring.
        sources.append((_parse_diff(diff), change.rev or "HEAD"))

    if change.worktree:
        args = ["diff", f"-U{context}", "--no-color", "--no-ext-diff", "-M"]
        args.append("--cached" if change.staged_only else "HEAD")
        diff = git(root, *args, check=False)
        sources.append((_parse_diff(diff), None))

    blocks: list[dict] = []
    skipped: list[str] = []
    seen: set[tuple[str, int, int]] = set()
    total_lines = 0
    truncated = False

    for hunks, rev in sources:
        for hunk in _merge(hunks):
            if includes and not _matches(hunk.path, includes):
                continue
            if _matches(hunk.path, excludes):
                if hunk.path not in skipped:
                    skipped.append(hunk.path)
                continue

            lines = _file_lines(root, hunk.path, rev)
            if lines is None:
                if hunk.path not in skipped:
                    skipped.append(hunk.path)
                continue

            start = max(1, hunk.start)
            end = min(hunk.end, len(lines))
            if end < start:
                continue
            if end - start + 1 > max_block_lines:
                end = start + max_block_lines - 1

            marker = (hunk.path, start, end)
            if marker in seen:
                continue

            if len(blocks) >= max_blocks or total_lines + (end - start + 1) > max_total_lines:
                truncated = True
                if hunk.path not in skipped:
                    skipped.append(hunk.path)
                continue

            seen.add(marker)
            total_lines += end - start + 1
            blocks.append({
                "filename": hunk.path,
                "language": language_for(hunk.path),
                "line_start": start,
                "line_end": end,
                "content": "\n".join(lines[start - 1:end]),
            })

    return BlockResult(blocks=blocks, skipped=skipped, truncated=truncated)


def diffstat(change: ChangeSet) -> str:
    if change.worktree_only:
        args = ["diff", "--stat", "--cached"] if change.staged_only else ["diff", "--stat", "HEAD"]
        return git(change.root, *args, check=False).strip()
    return git(change.root, "diff", "--stat", change.range, check=False).strip()


def auto_title(change: ChangeSet) -> str:
    """A title derived from the change itself, used when none is supplied."""
    if change.commits:
        newest = change.commits[0]
        subject = newest.split(" ", 1)[1] if " " in newest else newest
        return f"Review: {subject}"[:200]
    return f"Review: working tree changes in {change.root.name}"[:200]


def auto_description(change: ChangeSet, result: BlockResult) -> str:
    """A description derived from commit subjects and the diffstat."""
    parts: list[str] = [f"Change set: `{change.description}`."]
    if change.commits:
        parts.append("\n**Commits**\n")
        parts.append("\n".join(f"- {c}" for c in change.commits))
    stat = diffstat(change)
    if stat:
        parts.append("\n**Diffstat**\n")
        parts.append(f"```\n{stat}\n```")
    if result.skipped:
        shown = ", ".join(f"`{p}`" for p in result.skipped[:10])
        more = f" (+{len(result.skipped) - 10} more)" if len(result.skipped) > 10 else ""
        why = "size cap" if result.truncated else "generated/binary"
        parts.append(f"\nNot quoted ({why}): {shown}{more}")
    return "\n".join(parts)
