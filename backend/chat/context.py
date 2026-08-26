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


def _header(ticket: Ticket, names: dict[int, str]) -> str:
    def who(uid):
        return names.get(uid, f"#{uid}") if uid else "unassigned"

    lines = [
        f"# Ticket #{ticket.id}: {ticket.title}",
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
        f"- tags: {', '.join(ticket.tags or []) or 'none'}",
    ]
    return "\n".join(lines)


def _description(ticket: Ticket, budget: Budget) -> str:
    body = (ticket.description or "").strip()
    if not body:
        return ""
    text = budget.take(body, cap=DESCRIPTION_CAP)
    return f"\n\n## Description\n\n{text}" if text else ""


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
    text = budget.take("\n".join(lines))
    return f"\n\n## Resolver agent runs\n\n{text}" if text else ""


def _code_blocks(ticket: Ticket, budget: Budget) -> str:
    blocks = ticket.code_blocks or []
    if not blocks:
        return ""
    section = Budget(min(budget.remaining, CODE_SECTION_CAP))
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        content = section.take(str(block.get("content") or ""), cap=CODE_BLOCK_CAP)
        if not content:
            break  # section budget exhausted; further blocks would render empty
        lang = block.get("language") or ""
        parts.append(
            f"### {block.get('filename') or 'unknown'} "
            f"(lines {block.get('line_start')}–{block.get('line_end')})\n\n"
            f"```{lang}\n{content}\n```"
        )
    if not parts:
        return ""
    budget.used += section.used
    return "\n\n## Code\n\n" + "\n\n".join(parts)


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
    text = budget.take("\n\n---\n\n".join(parts), cap=COMMENTS_CAP)
    return f"\n\n## Comments\n\n{text}" if text else ""


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
    lines = [
        f"- {_fmt_date(a.created_at)} — {names.get(a.actor_id, f'#{a.actor_id}')} "
        f"{a.action} {a.detail if a.detail else ''}".rstrip()
        for a in reversed(rows)
    ]
    text = budget.take("\n".join(lines))
    return f"\n\n## Activity\n\n{text}" if text else ""


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
    # lost its own ticket id would be worse than useless.
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
