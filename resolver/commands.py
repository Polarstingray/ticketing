"""Structured prompt commands for the resolver.

A *command* is a named, premade prompt that a ticket can invoke with a single
slash-command line in its body (or a human comment), e.g. `/security-audit`.
When the resolver detects one it injects the matching template as the ticket's
primary objective and runs the normal plan/implement (or review) lifecycle.

Commands live as Markdown files with a small YAML-ish frontmatter block under
`resolver/commands/*.md`:

    ---
    type: code_review        # code_review | task  (controls routing; default task)
    description: Security audit of the target repo
    priority: high           # optional hint, not enforced
    ---
    <the premade prompt body that becomes the ticket's objective>

Detection is deterministic — the model is never in the loop deciding which
command ran, mirroring the `/ticket` directive scanner in resolve_tickets.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

COMMANDS_DIR = Path(__file__).resolve().parent / "commands"

# A command name is a lowercase slug: letters/digits/hyphens. This is what
# appears after the slash and must match a `<name>.md` file in COMMANDS_DIR.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Slash-verbs the resolver already gives its own meaning — never treat these as
# premade-prompt commands (see process() dispatch in resolve_tickets.py).
RESERVED = frozenset({"ticket", "approve", "revise", "review"})

VALID_TYPES = frozenset({"task", "code_review"})


@dataclass
class Command:
    """A loaded premade-prompt command."""
    name: str
    type: str            # "task" | "code_review"
    description: str
    priority: str        # "" when unset
    body: str            # the prompt text injected as the ticket's objective


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a `---`-delimited frontmatter block off the front of `text`.

    Dependency-free (no PyYAML): only flat `key: value` lines are supported,
    which is all the command format needs. Returns (metadata, body). If there's
    no frontmatter, metadata is empty and the whole text is the body.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip("\n")
    meta: dict[str, str] = {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body = "\n".join(lines[i + 1:]).strip("\n")
            return meta, body
        line = lines[i].strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip().lower()] = val.strip().strip("'\"")
    # No closing `---`: treat the whole thing as body, ignore the partial block.
    return {}, text.strip("\n")


def load_command(name: str) -> Command | None:
    """Load `commands/<name>.md`. Returns None if the name is invalid, the file
    is missing, or its declared `type` is not recognized."""
    if not _NAME_RE.match(name) or name in RESERVED:
        return None
    path = COMMANDS_DIR / f"{name}.md"
    if not path.is_file():
        return None
    meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    if not body.strip():
        return None
    ctype = (meta.get("type") or "task").strip()
    if ctype not in VALID_TYPES:
        return None
    return Command(
        name=name,
        type=ctype,
        description=meta.get("description", ""),
        priority=meta.get("priority", ""),
        body=body,
    )


def available_commands() -> list[str]:
    """Sorted names of all loadable commands — used in error messages."""
    if not COMMANDS_DIR.is_dir():
        return []
    names = []
    for p in sorted(COMMANDS_DIR.glob("*.md")):
        if p.stem == "README":
            continue
        if load_command(p.stem) is not None:
            names.append(p.stem)
    return names


def _command_slug(line: str) -> str | None:
    """If `line` is a bare slash-command (`/name` optionally followed by free
    text), return the slug; else None. The slug must look like a command name
    and not be a reserved verb."""
    if not line.startswith("/"):
        return None
    token = line[1:].split(None, 1)[0] if len(line) > 1 else ""
    token = token.lower()
    if not _NAME_RE.match(token) or token in RESERVED:
        return None
    return token


def detect_command(ticket: dict, comments: list[dict],
                   bot_id: int) -> tuple[Command | None, str | None]:
    """Scan the ticket body and human comments for the first slash-command line.

    Returns (command, unknown):
      - (Command, None)  a known command was found and loaded;
      - (None, "<slug>") a `/<slug>` line looked like a command but matched no
                         template (so the caller can report available ones);
      - (None, None)     no command line at all.

    Bot-authored text is skipped (mirrors collect_directives) so the resolver
    never parses its own output. The ticket body wins over comments, and within
    a source the first command line wins.
    """
    sources: list[str] = [ticket.get("description") or ""]
    for c in comments:
        if c.get("author") != bot_id:
            sources.append(c.get("body") or "")

    for text in sources:
        for raw in text.splitlines():
            slug = _command_slug(raw.strip())
            if slug is None:
                continue
            cmd = load_command(slug)
            if cmd is not None:
                return cmd, None
            return None, slug
    return None, None
