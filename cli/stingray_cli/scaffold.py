"""Project scaffolding: render a template, find its stubs, and file their tickets.

The stub convention is two lines, so it is both machine-scannable and fatal at
runtime if you forget to implement it:

    def charge_card(token: str, cents: int) -> str:
        \"\"\"Charge `token` for `cents`; returns the provider charge id.\"\"\"
        # STINGRAY-STUB: implement against the payment provider.
        # ACCEPTANCE: idempotent per token; raises on a declined card.
        raise NotImplementedError("STINGRAY-STUB")

Only the ``STINGRAY-STUB:`` comment triggers a ticket, via a comment-syntax
agnostic regex, so a template in any language works.
"""
from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

STUB_MARKER = "STINGRAY-STUB"
_STUB_RE = re.compile(r"(?://|#|\*|--|;)\s*STINGRAY-STUB:\s*(.+?)\s*$")
_ACCEPTANCE_RE = re.compile(r"(?://|#|\*|--|;)\s*ACCEPTANCE:\s*(.+?)\s*$")

# Where a stub's enclosing definition starts, per language family.
_DEF_RE = re.compile(
    r"^\s*(?:(?:async\s+)?def\s+|class\s+|(?:export\s+)?(?:async\s+)?function\s+"
    r"|func\s+|fn\s+|(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+\w+\s*\()"
)

MAX_BLOCK_LINES = 120

# Only source files are scanned for stubs. Prose files are excluded on purpose:
# a project's own CLAUDE.md documents the convention and therefore *contains* the
# marker, which would otherwise file a ticket against the documentation.
_CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java", ".kt",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".php", ".swift", ".scala",
    ".sh", ".bash", ".sql", ".css", ".scss", ".html", ".vue", ".svelte",
}

# A comment line, for stitching a wrapped STINGRAY-STUB/ACCEPTANCE note back
# together. Templates wrap at 88 columns like any other code.
_COMMENT_RE = re.compile(r"^\s*(?://|#|\*|--|;)\s?(.*)$")


class ScaffoldError(Exception):
    """A scaffold could not be produced."""


@dataclass
class Template:
    name: str
    path: Path
    description: str = ""
    notes: str = ""


@dataclass
class Stub:
    path: str          # repo-relative
    line: int          # 1-indexed line of the marker
    summary: str       # text after "STINGRAY-STUB:"
    acceptance: str = ""
    block_start: int = 0
    block_end: int = 0


def available_templates() -> list[Template]:
    if not TEMPLATES_DIR.is_dir():
        return []
    out = []
    for entry in sorted(TEMPLATES_DIR.iterdir()):
        if (entry / "files").is_dir():
            out.append(load_template(entry.name))
    return out


def load_template(name: str) -> Template:
    path = TEMPLATES_DIR / name
    if not (path / "files").is_dir():
        known = ", ".join(t.name for t in available_templates()) or "none"
        raise ScaffoldError(f"unknown template {name!r} (available: {known})")
    meta: dict = {}
    manifest = path / "template.toml"
    if manifest.is_file():
        meta = tomllib.loads(manifest.read_text(encoding="utf-8"))
    return Template(
        name=name,
        path=path,
        description=meta.get("description", ""),
        notes=meta.get("notes", ""),
    )


def _substitute(text: str, variables: dict[str, str]) -> str:
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def render(template: Template, dest: Path, variables: dict[str, str]) -> list[Path]:
    """Copy the template tree into ``dest``, substituting in paths and content.

    A ``.tmpl`` suffix is stripped after substitution, so a template can ship a
    file that would otherwise be picked up by its own tooling.
    """
    source = template.path / "files"
    written: list[Path] = []
    for item in sorted(source.rglob("*")):
        rel = item.relative_to(source)
        target = dest / Path(_substitute(str(rel), variables))
        if target.name.endswith(".tmpl"):
            target = target.with_name(target.name[:-5])
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = item.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            target.write_bytes(item.read_bytes())
        else:
            target.write_text(_substitute(content, variables), encoding="utf-8")
        written.append(target)
    return written


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


def scan_stubs(root: Path) -> list[Stub]:
    """Every ``STINGRAY-STUB:`` marker under ``root``, with its enclosing block."""
    stubs: list[Stub] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix.lower() not in _CODE_SUFFIXES:
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
                path=str(path.relative_to(root)),
                line=idx + 1,
                summary=summary,
                acceptance=acceptance,
                block_start=start,
                block_end=end,
            ))
    return stubs


def git_init_and_commit(dest: Path, message: str) -> None:
    """Initialize the repo and make the scaffold commit.

    This must happen *before* any ticket is filed: code blocks carry line
    numbers, and filing against an uncommitted tree that then changes makes every
    filed range wrong.
    """
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(dest), *args],
                              capture_output=True, text=True)

    if (dest / ".git").exists():
        raise ScaffoldError(f"{dest} is already a git repository")
    if run("init").returncode != 0:
        raise ScaffoldError("git init failed")
    run("branch", "-M", "main")
    run("add", "-A")
    committed = run("commit", "-m", message)
    if committed.returncode != 0:
        raise ScaffoldError(
            "git commit failed — is user.name/user.email configured?\n"
            + committed.stderr.strip()
        )


def validate_tree(dest: Path) -> list[str]:
    """Sanity-check a tree an agent may have rewritten. Returns problems found."""
    problems: list[str] = []
    for path in sorted(dest.rglob("*.py")):
        if ".git" in path.parts:
            continue
        try:
            import ast
            ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(f"{path.relative_to(dest)}: {exc}")
    for path in sorted(dest.rglob("*.json")):
        if ".git" in path.parts:
            continue
        try:
            import json
            json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            problems.append(f"{path.relative_to(dest)}: {exc}")
    if not scan_stubs(dest):
        problems.append(f"no {STUB_MARKER} markers left — a scaffold with no stubs "
                        "is a failure, not a success")
    return problems
