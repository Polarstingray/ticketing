"""Building the context pack — the ticket facts the model is allowed to see.

The pack is Markdown rather than JSON: it is read by a language model, and prose
with headings costs fewer tokens than the same facts wrapped in braces and quoted
keys.

**The permission rule is the whole point of this module.** ``ticket_pack`` takes
the calling ``User`` and goes through ``auth.can_view_ticket`` — the same gate
``GET /tickets/{id}`` uses — returning ``None`` for a ticket the caller may not
see. The router turns that into a 404, never a 403, so the assistant cannot be
used to confirm the existence of someone else's ticket.

Everything in the pack originates as user- or agent-authored text and must be
treated as untrusted data by the caller; see ``prompts.py``.
"""
from sqlalchemy.orm import Session

from auth import can_view_ticket
from models import Activity, AgentRun, Comment, Ticket, User

from .budget import Budget

# Per-section caps, drawn from the shared budget. They exist so one huge section
# can't starve the rest: a 3,000-line code block is informative, but not at the
# cost of every comment on the ticket.
CODE_BLOCK_CAP = 6_000     # per individual block
CODE_SECTION_CAP = 20_000  # all blocks together
COMMENTS_CAP = 20_000
DESCRIPTION_CAP = 12_000

# A section is only worth its heading if some of its content survives, so a
# section is dropped rather than rendered as a title over two words of text.
MIN_SECTION_CONTENT = 80
MIN_CODE_CONTENT = 200

# The header is charged against the budget but never clipped (see ``ticket_pack``),
# so the header fields that are unbounded in the schema bound themselves here.
# Without these, a ticket with a 20KB title produces a 20KB overshoot.
TITLE_CAP = 200
TAGS_CAP = 400
NAME_CAP = 80

# Row limits for the naturally-unbounded sections. Newest-first at the source,
# re-sorted oldest-first for the pack so the model reads them chronologically.
MAX_COMMENTS = 40
MAX_ACTIVITY = 40
MAX_RUNS = 40


def _fmt_date(value) -> str:
    return value.isoformat() if value else "—"


def _names(db: Session, ids: set[int]) -> dict[int, str]:
    """Display names for a set of user ids, for rendering authors and actors.

    One query rather than one per row — the same batching the activity feed does.
    Ids with no surviving user fall back to ``#<id>`` at the call site.
    """
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = db.query(User.id, User.display_name).filter(User.id.in_(ids)).all()
    return {uid: name for uid, name in rows}


def _field(value, limit: int) -> str:
    """One header field, collapsed onto a single line and bounded.

    A marked truncation would be worse than useless inside a Markdown heading —
    the note is multi-line — so an over-long field ends in an ellipsis instead.
    """
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _header(ticket: Ticket, names: dict[int, str]) -> str:
    def who(uid):
        if not uid:
            return "unassigned"
        return _field(names.get(uid, f"#{uid}"), NAME_CAP)

    lines = [
        f"# Ticket #{ticket.id}: {_field(ticket.title, TITLE_CAP)}",
        "",
        f"- type: {ticket.type}",
        f"- status: {ticket.status}",
        f"- priority: {ticket.priority}",
        f"- created by: {who(ticket.created_by)}",
        f"- assigned to: {who(ticket.assigned_to)}",
        f"- created: {_fmt_date(ticket.created_at)}",
        f"- updated: {_fmt_date(ticket.updated_at)}",
        f"- due: {_fmt_date(ticket.due_date)}",
        f"- archived: {bool(ticket.archived)}",
        f"- tags: {_field(', '.join(ticket.tags or []), TAGS_CAP) or 'none'}",
    ]
    return "\n".join(lines)


def _section(budget: Budget, heading: str, body: str, *, cap: int | None = None) -> str:
    """One ``## heading`` plus its body, both charged against the budget.

    The heading is part of what the model is sent, so it is paid for like the
    body is; a section that can't afford its heading and a useful amount of text
    is dropped whole rather than rendered as a title over two words.
    """
    if budget.remaining < len(heading) + MIN_SECTION_CONTENT:
        return ""
    budget.used += len(heading)
    return heading + budget.take(body, cap=cap)


def _description(ticket: Ticket, budget: Budget) -> str:
    body = (ticket.description or "").strip()
    if not body:
        return ""
    return _section(budget, "\n\n## Description\n\n", body, cap=DESCRIPTION_CAP)


def _agent_runs(db: Session, ticket: Ticket, budget: Budget) -> str:
    """The resolver's per-phase runs — the raw material for debugging a run.

    Deliberately placed before the long free-text sections: it is a handful of
    short rows, and it is the section a question like "why did the resolver stop
    on this ticket?" actually needs. A later phase attaches the failing run's log
    tail here (see docs/chat-design.md); today the app stores only metadata, so
    that is exactly what this reports.
    """
    runs = (
        db.query(AgentRun)
        .filter(AgentRun.ticket_id == ticket.id)
        .order_by(AgentRun.started_at.asc().nullslast(), AgentRun.id.asc())
        .limit(MAX_RUNS)
        .all()
    )
    if not runs:
        return ""
    lines = ["| phase | agent | model | status | in | out | cost |",
             "|---|---|---|---|---|---|---|"]
    for r in runs:
        lines.append(
            f"| {r.phase} | {r.agent} | {r.model or '—'} | {r.status} | "
            f"{r.input_tokens} | {r.output_tokens} | ${r.cost_usd:.4f} |"
        )
    return _section(budget, "\n\n## Resolver agent runs\n\n", "\n".join(lines))


def _code_blocks(ticket: Ticket, budget: Budget) -> str:
    """Code blocks under a nested budget, so the section total is capped too.

    The nested ``Budget`` is charged for everything this section emits — heading,
    per-block titles, fences and separators, not just the code inside them — and
    its total is then charged to the parent. Charging only the content would let
    a ticket with many small blocks overrun the pack in Markdown chrome.
    """
    blocks = ticket.code_blocks or []
    if not blocks:
        return ""
    heading = "\n\n## Code\n\n"
    separator = "\n\n"
    section = Budget(min(budget.remaining, CODE_SECTION_CAP))
    if section.remaining < len(heading) + MIN_CODE_CONTENT:
        return ""
    section.used += len(heading)

    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        content = str(block.get("content") or "")
        if not content.strip():
            continue
        prefix = (
            f"### {block.get('filename') or 'unknown'} "
            f"(lines {block.get('line_start')}–{block.get('line_end')})\n\n"
            f"```{block.get('language') or ''}\n"
        )
        suffix = "\n```"
        reserve = len(prefix) + len(suffix) + (len(separator) if parts else 0)
        if section.remaining < reserve + MIN_CODE_CONTENT:
            break  # no room left for a header and a useful amount of code
        section.used += reserve
        parts.append(prefix + section.take(content, cap=CODE_BLOCK_CAP) + suffix)
    if not parts:
        return ""
    budget.used += section.used
    return heading + separator.join(parts)


def _comments(db: Session, ticket: Ticket, names: dict[int, str], budget: Budget) -> str:
    rows = (
        db.query(Comment)
        .filter(Comment.ticket_id == ticket.id)
        .order_by(Comment.created_at.desc(), Comment.id.desc())
        .limit(MAX_COMMENTS)
        .all()
    )
    if not rows:
        return ""
    # Newest-first from SQL (so the limit keeps the *recent* ones), then reversed
    # so the model reads the thread in the order it happened.
    parts = [
        f"**{names.get(c.author, f'#{c.author}')}** at {_fmt_date(c.created_at)}:\n\n"
        f"{(c.body or '').strip()}"
        for c in reversed(rows)
    ]
    return _section(budget, "\n\n## Comments\n\n", "\n\n---\n\n".join(parts),
                    cap=COMMENTS_CAP)


def _activity(db: Session, ticket: Ticket, names: dict[int, str], budget: Budget) -> str:
    rows = (
        db.query(Activity)
        .filter(Activity.ticket_id == ticket.id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(MAX_ACTIVITY)
        .all()
    )
    if not rows:
        return ""
    # Newest-first from SQL then reversed, exactly as _comments does: the LIMIT
    # keeps the *recent* rows, the pack reads in the order things happened.
    lines = [
        f"- {_fmt_date(a.created_at)} — {names.get(a.actor_id, f'#{a.actor_id}')} "
        f"{a.action} {a.detail if a.detail else ''}".rstrip()
        for a in reversed(rows)
    ]
    return _section(budget, "\n\n## Activity\n\n", "\n".join(lines))


def ticket_pack(db: Session, user: User, ticket_id: int, *, budget: int) -> str | None:
    """Markdown context for one ticket, or ``None`` if ``user`` may not view it.

    ``None`` also covers "no such ticket": the caller renders both as 404, so the
    two are deliberately indistinguishable to a client.

    Sections are assembled in descending priority — header, description, agent
    runs, code, comments, activity — each drawing from a shared character budget,
    so an oversized ticket loses its activity tail rather than its identity.
    """
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None or not can_view_ticket(user, ticket):
        return None

    actor_ids = {ticket.created_by, ticket.assigned_to}
    actor_ids |= {c.author for c in ticket.comments}
    actor_ids |= {a.actor_id for a in ticket.activities}
    names = _names(db, actor_ids)

    allowance = Budget(budget)
    # The header is charged against the budget but never clipped: a pack that has
    # lost its own ticket id would be worse than useless. The overshoot that buys
    # is bounded, not open-ended — every header field that is unbounded in the
    # schema (title, tags, display names) is bounded by ``_field``, so the header
    # is a few hundred characters however large the ticket is.
    header = _header(ticket, names)
    allowance.used += len(header)

    return "".join([
        header,
        _description(ticket, allowance),
        _agent_runs(db, ticket, allowance),
        _code_blocks(ticket, allowance),
        _comments(db, ticket, names, allowance),
        _activity(db, ticket, names, allowance),
    ])
