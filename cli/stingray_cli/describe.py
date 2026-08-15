"""The optional ``--describe`` pass: let a local agent write the ticket's prose.

Everything here is best-effort by design. A description bot that blocks you from
filing a ticket is worse than no description bot, so every failure path falls
back to the deterministic git-derived text unless ``--require-describe`` says
otherwise.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field

from stingray_cli import gitctx
from stingray_cli.agent import AgentError
from stingray_cli.agent import run as run_agent
from stingray_client.tickets import PRIORITIES, is_reserved_tag

# How much diff to show the model. Beyond this the signal is mostly noise and
# the prompt starts costing real money.
MAX_DIFF_CHARS = 40_000
# The diffstat is normally tiny, but a thousand-file change would otherwise
# bloat the prompt as much as the diff itself.
MAX_STAT_CHARS = 4_000

# Generous on purpose. An agent CLI's *startup* can dominate its API time — on a
# cold `claude` invocation here, a trivial prompt took 59s wall for 3s of API —
# and a real diff prompt measured 350s end to end. A too-tight default doesn't
# fail loudly, it just silently falls back to the commit-derived text, which
# looks like "--describe did nothing". Override per profile:
#     [profile.<name>.describe]
#     timeout = 900
DEFAULT_TIMEOUT = 900

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class DescribeError(Exception):
    """The agent ran but produced nothing usable."""


@dataclass
class Suggestion:
    title: str
    description: str
    priority: str = ""
    tags: list[str] = field(default_factory=list)


def build_prompt(change: gitctx.ChangeSet) -> str:
    """Give the model the change, not the repo — commits, diffstat, and diff."""
    root = change.root
    if change.worktree_only:
        args = (["diff", "--cached"] if change.staged_only else ["diff", "HEAD"])
    else:
        args = ["diff", change.range]
    diff = gitctx.git(root, *args, check=False)
    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS]

    parts = [
        "You are preparing a code-review ticket for a change in the repository "
        f"'{root.name}'. Summarize what changed and what a reviewer should scrutinize.",
        "",
        f"Change set: {change.description}",
    ]
    if change.commits:
        parts += ["", "Commits:", *(f"  {c}" for c in change.commits)]
    stat = gitctx.diffstat(change)
    if stat:
        parts += ["", "Diffstat:", stat[:MAX_STAT_CHARS]]
    parts += ["", "Diff:" + (" (TRUNCATED)" if truncated else ""), diff]
    parts += [
        "",
        "Reply with a single fenced ```json block and nothing else:",
        '{"title": "...", "description": "...", "priority": "low|medium|high|critical",',
        ' "tags": ["backend"]}',
        "",
        "Rules:",
        "- title: imperative, <= 100 chars, no 'Review:' prefix (it is added later).",
        "- description: markdown. What changed, why, and what to scrutinize.",
        "- priority: judge from risk, not size.",
        "- tags: at most 5 lowercase area tags like 'backend' or 'auth'. Never tags "
        "starting with claude:, resolver:, repo:, parent: or review-by:, and never "
        "'dangerous', 'fix' or 'delegate'.",
        "- Ground it in the diff. Do not invent changes you cannot see.",
    ]
    return "\n".join(parts)


def parse_response(text: str) -> Suggestion:
    """Pull a Suggestion out of the model's output, strictly then leniently."""
    data = _find_json(text)
    if data is None:
        raise DescribeError("no JSON object found in the agent's output")

    title = str(data.get("title") or "").strip()
    if not title:
        raise DescribeError("the agent returned no title")
    title = title[:120]

    description = str(data.get("description") or "").strip()[:8000]

    priority = str(data.get("priority") or "").strip().lower()
    if priority not in PRIORITIES:
        if priority:
            print(f"note: ignoring unknown priority {priority!r} from the agent",
                  file=sys.stderr)
        priority = ""

    # A hallucinated `dangerous` or `repo:evil` must never reach the payload.
    tags: list[str] = []
    for raw in data.get("tags") or []:
        tag = str(raw).strip().lower()
        if not tag or len(tag) > 40 or is_reserved_tag(tag) or tag in tags:
            continue
        tags.append(tag)
        if len(tags) == 5:
            break

    return Suggestion(title=title, description=description, priority=priority, tags=tags)


def _find_json(text: str) -> dict | None:
    """Whole output, then the last ```json fence, then the outermost braces."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fences = _FENCE_RE.findall(text)
    for candidate in reversed(fences):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def describe_change(change: gitctx.ChangeSet, *, agent: str | None = None,
                    required: bool = False, profile=None) -> Suggestion | None:
    """Run the description pass. Returns None when it fails and isn't required."""
    settings = dict(getattr(profile, "describe", None) or {})
    agent = agent or settings.get("agent") or None
    model = settings.get("model") or None
    timeout = int(settings.get("timeout", DEFAULT_TIMEOUT))

    try:
        output = run_agent(build_prompt(change), change.root,
                           agent=agent, model=model, timeout=timeout)
        return parse_response(output)
    except (AgentError, DescribeError) as exc:
        if required:
            raise
        print(f"warning: --describe failed ({exc}); "
              "using the commit-derived description instead", file=sys.stderr)
        return None
