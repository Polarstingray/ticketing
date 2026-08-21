"""Guided projects: the assignment handout and the exercise prose for its tickets.

A plain scaffold gives you stubs and a ticket per stub. A *guided* scaffold adds
the thing that makes it feel like a CS-class project: a handout with learning
goals, ordered milestones and a rubric, plus ticket bodies written as exercises
rather than as the raw marker text.

Two artifacts come out of the agent pass:

- ``ASSIGNMENT.md`` — the handout. Written to the project root and added to
  ``.gitignore``, so a learner who pushes their work doesn't publish the brief
  (and an instructor can hand out a different one against the same repo). Its
  substance is mirrored into the epic ticket, so deleting the file loses nothing.
- ``.stingray-exercises.json`` — a scratch side-channel mapping ``path:line`` to
  the prose for that stub's ticket. Read once, then deleted; it never reaches the
  project or a commit.

Every step here is best-effort. An agent that times out, writes nothing, or emits
malformed JSON degrades to a deterministic handout built from the scanned stubs —
never to a failed scaffold.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from stingray_client.stubs import STUB_MARKER, Stub

ASSIGNMENT_FILE = "ASSIGNMENT.md"
EXERCISES_FILE = ".stingray-exercises.json"

COURSE_LEVELS = ("intro", "intermediate", "advanced")

# How much of the design to hand over, per level. The stubs themselves are the
# same; what changes is how much scaffolding the prose provides around them.
_LEVEL_GUIDANCE = {
    "intro": (
        "Audience: a first- or second-year student. Spell out the data flow and "
        "name the concepts they need to look up. Order milestones so each one is "
        "runnable and testable on its own. Prefer many small stubs over few large "
        "ones. Do not assume familiarity with the frameworks in use."
    ),
    "intermediate": (
        "Audience: a student who has written a few projects. State the "
        "requirements and the constraints, but let them choose the data "
        "structures and the decomposition. Name the tradeoff in each milestone "
        "without resolving it for them."
    ),
    "advanced": (
        "Audience: a senior student or a strong self-learner. Give requirements "
        "and invariants only. Leave the architecture open, and make at least one "
        "milestone an explicit design decision with no single right answer."
    ),
}


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def assignment_prompt(
    *,
    project_name: str,
    template_description: str,
    template_notes: str,
    intent: str | None,
    stubs: list[Stub],
    level: str,
    milestones: int,
) -> str:
    """The prompt for the agent pass that writes the handout and the exercises."""
    stub_lines = "\n".join(
        f"  {s.path}:{s.line} — {s.summary}"
        + (f" (acceptance: {s.acceptance})" if s.acceptance else "")
        for s in stubs
    ) or "  (none found)"

    return "\n".join([
        f"You are writing the assignment handout for a guided programming project "
        f"called '{project_name}'. The code skeleton already exists in this "
        f"directory; your job is the *coursework* around it, not the code.",
        "",
        f"What the project is: {intent or template_description}",
        f"Template notes: {template_notes or '(none)'}",
        "",
        _LEVEL_GUIDANCE.get(level, _LEVEL_GUIDANCE["intermediate"]),
        "",
        f"The skeleton contains {len(stubs)} `{STUB_MARKER}` marker(s). Each one is "
        "already a ticket in the learner's tracker:",
        stub_lines,
        "",
        "Write exactly two files. Do not modify any source file — not one line.",
        "",
        f"1. `{ASSIGNMENT_FILE}` at the project root, with these sections in this order:",
        "",
        f"   # {project_name}",
        "   ## Learning goals",
        "       3-6 bullets. What the learner will be able to do afterwards, in terms",
        "       of transferable skills, not of this codebase's file names.",
        "   ## What you are building",
        "       One or two paragraphs. The brief, as a client would state it.",
        "   ## Milestones",
        f"       Exactly {milestones} sections, ordered so each builds on the last.",
        "       Each: a goal sentence, the stubs it covers (as `path:line` bullets,",
        "       using the real paths listed above), and a '**Done when:**' line",
        "       stating an observable outcome.",
        "   ## Rubric",
        "       A markdown table of criteria against levels, with a weight column",
        "       whose weights sum to 100.",
        "   ## Getting started",
        "       The actual install/run/test commands for this project. Read the",
        "       README, the packaging files and CLAUDE.md to get them right — do not",
        "       invent commands.",
        "",
        f"2. `{EXERCISES_FILE}` at the project root: JSON, one entry per marker above.",
        "",
        '   {"exercises": [',
        '     {"stub": "<path>:<line>", "title": "<short imperative title>",',
        '      "body": "<2-4 sentences of markdown: what this piece must do, why it',
        '               matters in the project, and what to watch out for>"}',
        "   ]}",
        "",
        "   Use the exact `path:line` strings listed above as the `stub` keys.",
        "",
        "Rules — follow all of them:",
        "- Do NOT give away implementations. Describe what and why, never how. No",
        "  code snippets, no pseudocode, no algorithm walkthroughs, no library call",
        "  sequences. If a stub has an obvious one-line answer, say what property the",
        "  answer must have instead of stating it.",
        "- Every stub listed above must appear in exactly one milestone and have",
        f"  exactly one entry in `{EXERCISES_FILE}`.",
        "- Ground the handout in what is actually in this directory. Read the files.",
        "- Write prose a student reads once and then starts working. No filler.",
    ])


def deterministic_assignment(
    *,
    project_name: str,
    template_description: str,
    template_notes: str,
    intent: str | None,
    stubs: list[Stub],
    milestones: int,
) -> str:
    """A handout built from the scanned stubs alone, with no agent involved.

    This is what ``--no-ai`` produces and what an agent failure falls back to. It
    is deliberately honest about being generated: it organizes the real work into
    milestones and states the acceptance criteria the markers already carry, and
    it does not pretend to learning goals it cannot know.
    """
    out = [
        f"# {project_name}",
        "",
        "## What you are building",
        "",
        intent or template_description or f"The {project_name} project.",
        "",
    ]
    if template_notes:
        out += [template_notes, ""]

    out += [
        "## How this works",
        "",
        f"Every place you need to write code is marked `{STUB_MARKER}:` in the source "
        "and has its own ticket in Stingray. The marker says what to implement; the "
        "`ACCEPTANCE:` line below it says how to know you are done — treat it as the "
        "test to write. The code raises `NotImplementedError` until you replace it, "
        "so nothing silently half-works.",
        "",
        "## Milestones",
        "",
    ]

    if not stubs:
        out += ["_No stubs found._", ""]
    else:
        # Split the stubs into contiguous groups, keeping each file's stubs
        # together: the scan is path-sorted, so this yields milestones that move
        # through the project one module at a time.
        groups = _chunk(stubs, milestones)
        for index, group in enumerate(groups, start=1):
            files = sorted({s.path for s in group})
            out += [
                f"### Milestone {index} — {', '.join(files)}",
                "",
            ]
            for stub in group:
                out.append(f"- `{stub.path}:{stub.line}` — {stub.summary}")
            done = [s.acceptance for s in group if s.acceptance]
            out += ["", "**Done when:**", ""]
            # One bullet per acceptance criterion. Joining them into a sentence
            # reads as a run-on, and these are separate testable claims.
            out += ([f"- {d}" for d in done] if done else
                    ["- every stub above is implemented and the project's tests pass."])
            out.append("")

    out += [
        "## Rubric",
        "",
        "| Criterion | Weight |",
        "| --- | --- |",
        "| Every stub implemented, no `NotImplementedError` left | 40 |",
        "| Acceptance criteria met, demonstrated by tests | 30 |",
        "| Code reads like the surrounding code | 20 |",
        "| Error and edge cases handled | 10 |",
        "",
        "## Getting started",
        "",
        "See `README.md` for the install and run commands, and `CLAUDE.md` for the "
        "conventions this project expects you to keep.",
        "",
    ]
    return "\n".join(out)


def _chunk(items: list, groups: int) -> list[list]:
    """Split ``items`` into at most ``groups`` near-equal contiguous chunks."""
    groups = max(1, min(groups, len(items)))
    size, extra = divmod(len(items), groups)
    out = []
    start = 0
    for i in range(groups):
        end = start + size + (1 if i < extra else 0)
        out.append(items[start:end])
        start = end
    return out


def read_exercises(root: Path, *, warn=_warn) -> dict[str, dict]:
    """The agent's per-stub prose, keyed ``path:line``. Empty if unusable.

    Consumes the file: it is a scratch side-channel and must never survive into
    the project or a commit.
    """
    path = root / EXERCISES_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["exercises"]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        warn(f"warning: ignoring unreadable {EXERCISES_FILE} ({exc}); "
             "ticket bodies will use the stub markers")
        entries = []
    finally:
        path.unlink(missing_ok=True)

    out: dict[str, dict] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("stub", "")).strip()
        if key:
            out[key] = entry
    return out


def exercise_for(exercises: dict[str, dict], stub: Stub) -> tuple[str | None, str | None]:
    """``(title, body)`` for a stub, or ``(None, None)`` if the agent skipped it."""
    entry = exercises.get(f"{stub.path}:{stub.line}")
    if not entry:
        return None, None
    title = (entry.get("title") or "").strip() or None
    body = (entry.get("body") or "").strip() or None
    return title, body


def ensure_gitignored(root: Path, name: str) -> None:
    """Add ``name`` to the project's ``.gitignore``, idempotently.

    The handout is deliberately not committed: it is coursework, not code, and on
    the resolver path it is delivered as a ticket comment instead (``git add -A``
    honours ``.gitignore``, so a committed handout would leak into every PR).
    """
    path = root / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        existing = ""
    if any(line.strip() == name for line in existing.splitlines()):
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = (
        f"{prefix}\n# The assignment handout is coursework, not code — keep it local.\n"
        f"{name}\n"
    )
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(block)
    except OSError as exc:
        _warn(f"warning: could not add {name} to .gitignore ({exc})")


def epic_summary(assignment: str, *, limit: int = 4000) -> str:
    """The part of the handout worth mirroring into the epic ticket.

    Everything down to the rubric — goals, brief and milestones — so the project
    survives someone deleting the local file. "Getting started" is dropped: it is
    the one section that is only useful next to a checkout.
    """
    lines = assignment.splitlines()
    kept: list[str] = []
    for line in lines:
        if line.strip().lower().startswith("## getting started"):
            break
        kept.append(line)
    text = "\n".join(kept).strip()
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n\n_(truncated — see ASSIGNMENT.md)_"
    return text
