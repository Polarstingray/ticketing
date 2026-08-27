r"""Shared ticket query builders — the read boundary, and exact tag matching.

These live outside ``routers/`` because they are not routing: they are the two
predicates that define *what a user is allowed to see* and *what counts as a tag
match*, and more than one subsystem needs them. The chat assistant's read-only
tools (``chat/tools.py``) start from :func:`visible_tickets` for exactly the same
reason every listing route does, and a tool importing a leading-underscore name
out of a router would both ignore that "private" signal and drag the whole router
import graph into ``chat/``.

Keeping them here means the reuse is explicit: there is one definition of the
read boundary, and anything that queries tickets on a user's behalf is expected
to be traceable back to it.
"""
from sqlalchemy import or_
from sqlalchemy.orm import Session

from auth import is_admin
from models import Ticket, User


def visible_tickets(db: Session, user: User):
    """Base query honoring the read boundary.

    Non-admins may only see tickets they created or are assigned to; code_review
    tickets embed private source in code_blocks. Every listing/aggregation route
    must start here, or it can leak the existence of another user's tickets.
    """
    query = db.query(Ticket)
    if not is_admin(user):
        query = query.filter(or_(Ticket.created_by == user.id, Ticket.assigned_to == user.id))
    return query


def tag_clause(tag: str):
    r"""SQL matching one exact tag inside the serialized JSON array.

    ``tags`` is a JSON column, which SQLite stores as text (``'["auth", "bug"]'``),
    so we match the quoted token. The surrounding quotes make this exact rather
    than a prefix match — the tag charset (schemas._TAG_CHARS) forbids ``"``, so
    ``"auth"`` cannot appear inside any other tag. LIKE wildcards still have to be
    escaped though: ``_`` is allowed in tags (it is in ``\w``) and would otherwise
    match any single character, so ``a_b`` would wrongly match ``axb``.
    """
    escaped = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return Ticket.tags.like(f'%"{escaped}"%', escape="\\")
