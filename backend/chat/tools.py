"""The assistant's read-only tools, and the inert action proposer.

**The whole security model is one line of signature:**

.. code-block:: python

    def dispatch(name, args, *, db, user, proposals, budget) -> str

``args`` is model-supplied and untrusted. ``db``, ``user``, ``proposals`` and
``budget`` are keyword-only and bound by the caller with ``functools.partial``,
so nothing the model emits can reach them. The model chooses *what* to ask
about; it never chooses *who is asking*. Every tool re-derives visibility from
the bound ``user`` through the same gates the REST API uses —
``ticket_queries.visible_tickets`` and ``auth.can_view_ticket`` — so a tool is
never a wider door than the endpoint beside it.

Two consequences worth stating, because they are easy to erode later:

* **Nothing here writes.** ``propose_action`` records a suggestion and returns;
  the user confirms it in the UI, which calls the existing endpoints as
  themselves. Ticket text is attacker-controllable, so the mitigation is
  structural — the write surface is zero — rather than a matter of prompt
  wording. A tool that wrote would move this feature from "no new authorization
  code" to "a second, weaker copy of it".
* **``dispatch`` never raises.** Every failure becomes an ``Error: ...`` string
  handed back to the model, which can then correct itself. An exception escaping
  here would 500 *mid-stream*, inside a response whose 200 status line has
  already been sent — the exact failure the router goes out of its way to avoid.
"""
import json
import logging

from sqlalchemy.orm import Session

from auth import can_view_ticket, is_admin
from control_tags import is_reserved_tag
from models import (
    AgentRun,
    ResolverInstance,
    Ticket,
    TicketPriority,
    TicketStatus,
    TicketType,
    User,
)
from ticket_queries import like_escape, tag_clause, visible_tickets

from .budget import Budget, clip
from .context import ticket_pack

logger = logging.getLogger(__name__)

# Both "no such ticket" and "not yours", deliberately identical — the same
# conflation ``ticket_pack`` and ``GET /tickets/{id}`` already make, so the
# assistant cannot be used to confirm that someone else's ticket exists.
NOT_FOUND = "Ticket not found."

SEARCH_LIMIT = 20        # rows, whatever the model asks for
TITLE_CAP = 120          # per row, so 20 rows can't be unbounded bytes
TAGS_CAP = 120
MAX_PROPOSALS = 5        # per turn; an injected description shouldn't yield a wall of cards
RATIONALE_CAP = 500
BODY_CAP = 4_000

# Per failed run, when reporting agent runs. Tight enough that two or three
# tails cannot consume a whole turn's budget on their own.
LOG_TAIL_CAP = 6_000
# A tail is not worth its heading if only a line or two of it survives.
MIN_TAIL_CONTENT = 200
_FENCE_OPEN = "```\n"
_FENCE_CLOSE = "\n```"

_STATUSES = [s.value for s in TicketStatus]
_TYPES = [t.value for t in TicketType]
_PRIORITIES = [p.value for p in TicketPriority]

PROPOSAL_KINDS = ("create_ticket", "add_comment", "request_fix", "set_status")

# What survives from a model-supplied payload, per kind. Everything else is
# discarded rather than rejected: a model that helpfully adds "created_by" should
# get a working card, not an error, and the field must not reach the endpoint.
_PAYLOAD_KEYS = {
    "create_ticket": ("type", "title", "description", "priority", "tags"),
    "add_comment": ("ticket_id", "body"),
    "request_fix": ("ticket_id",),
    "set_status": ("ticket_id", "status"),
}


# --- Argument coercion -------------------------------------------------------
# Models are loose with types: `"42"` for an int and `"true"` for a bool are
# routine. Coerce leniently, and fail with a sentence the model can act on.

class _BadArg(ValueError):
    """An argument that cannot be coerced. Carries the message for the model."""


def _int(args: dict, key: str, *, default=None):
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool):  # bool is an int subclass; never a ticket id
        raise _BadArg(f"`{key}` must be a number.")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _BadArg(f"`{key}` must be a number.") from None


def _str(args: dict, key: str, *, cap: int, default: str = "") -> str:
    value = args.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _BadArg(f"`{key}` must be text.")
    return clip(value.strip(), cap)


def _bool(args: dict, key: str, *, default: bool = False) -> bool:
    value = args.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _one_of(value: str, allowed: list[str], label: str) -> str:
    if value not in allowed:
        raise _BadArg(f"`{label}` must be one of: {', '.join(allowed)}.")
    return value


# --- Markdown rendering ------------------------------------------------------

def _cell(value, fallback: str = "") -> str:
    r"""One markdown table cell. ``|`` becomes ``\|`` or it ends the column.

    Every cell here is database text — titles, tags, bot names, resolver models —
    and none of it is constrained to exclude ``|``, so all of it is escaped on the
    way out rather than only the fields that seemed likely to contain one.
    """
    text = "" if value is None else str(value)
    return text.replace("|", "\\|") or fallback


# --- The read-only tools -----------------------------------------------------

def _search_tickets(args: dict, *, db: Session, user: User, budget: Budget, **_) -> str:
    """Find tickets this user may see.

    Starts from ``visible_tickets`` — non-negotiable, and the subject of its own
    test. ``assigned_to_me`` is the only filter that involves identity, and it
    takes it from the bound ``user``, never from ``args``.
    """
    query = visible_tickets(db, user)

    text = _str(args, "query", cap=200)
    if text:
        # Title only. `description` is the expensive column, and `get_ticket` is
        # the tool for depth — keeping search rows small is what makes several
        # hops affordable within one turn's budget.
        query = query.filter(Ticket.title.ilike(f"%{like_escape(text)}%", escape="\\"))

    status = _str(args, "status", cap=40)
    if status:
        query = query.filter(Ticket.status == _one_of(status, _STATUSES, "status"))

    tag = _str(args, "tag", cap=80)
    if tag:
        query = query.filter(tag_clause(tag))

    if _bool(args, "assigned_to_me"):
        query = query.filter(Ticket.assigned_to == user.id)

    limit = _int(args, "limit", default=10) or 10
    limit = max(1, min(SEARCH_LIMIT, limit))

    rows = query.order_by(Ticket.updated_at.desc(), Ticket.id.desc()).limit(limit).all()
    if not rows:
        # Never "": an empty tool result reads as a failure to some models, which
        # then retry the identical call and burn a hop.
        return "No tickets matched."

    names = _assignee_names(db, rows)
    lines = ["| id | status | priority | title | assignee | tags |",
             "|---|---|---|---|---|---|"]
    for t in rows:
        title = clip(_cell(t.title), TITLE_CAP)
        tags = clip(_cell(", ".join(t.tags or [])), TAGS_CAP)
        who = _cell(names.get(t.assigned_to) if t.assigned_to else None, "—")
        lines.append(
            f"| {t.id} | {t.status} | {t.priority} | {title} | {who} | {tags} |"
        )
    return budget.take("\n".join(lines))


def _assignee_names(db: Session, rows) -> dict[int, str]:
    """Display names for the assignees in a result set — one query, not N."""
    ids = {t.assigned_to for t in rows if t.assigned_to}
    if not ids:
        return {}
    users = db.query(User).filter(User.id.in_(ids)).all()
    return {u.id: (u.display_name or u.username) for u in users}


def _get_ticket(args: dict, *, db: Session, user: User, budget: Budget, **_) -> str:
    """The full context pack for one ticket — the same one the popup attaches.

    Bounded by what is left of the *turn's* budget rather than the per-pack
    default, so a ``get_ticket`` on the last hop cannot blow past what the
    earlier hops already spent.
    """
    ticket_id = _int(args, "ticket_id")
    if ticket_id is None:
        raise _BadArg("`ticket_id` is required.")
    pack = ticket_pack(db, user, ticket_id, budget=budget.remaining)
    if pack is None:
        return NOT_FOUND
    # The one tool that charges the budget by hand instead of via `budget.take`.
    # `take` clips what it is handed, but `ticket_pack` has already assembled the
    # pack to fit `budget.remaining`, section by section, so clipping it a second
    # time would cut mid-section. Charge for what it built, don't re-trim it.
    budget.used += len(pack)
    return pack


def _get_agent_runs(args: dict, *, db: Session, user: User, budget: Budget, **_) -> str:
    """The resolver's per-phase runs on one ticket.

    Gates on ``can_view_ticket`` itself rather than leaning on the caller: agent
    runs are keyed by ticket id, and reading them for a ticket you cannot see
    would leak both its existence and what the resolver did to it.

    A failed run's redacted transcript tail is appended below the table, newest
    first — that is the part that answers "why did implement fail on #42?", and
    it is why this tool is worth more than the summary already in the pack. A
    successful run has no tail; the resolver does not send one.
    """
    ticket_id = _int(args, "ticket_id")
    if ticket_id is None:
        raise _BadArg("`ticket_id` is required.")
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None or not can_view_ticket(user, ticket):
        return NOT_FOUND

    runs = (
        db.query(AgentRun)
        .filter(AgentRun.ticket_id == ticket.id)
        .order_by(AgentRun.started_at.asc().nullslast(), AgentRun.id.asc())
        .all()
    )
    if not runs:
        return f"No agent runs recorded on ticket #{ticket.id}."
    lines = ["| phase | agent | model | status | in | out | cost |",
             "|---|---|---|---|---|---|---|"]
    for r in runs:
        lines.append(
            f"| {_cell(r.phase, '—')} | {_cell(r.agent, '—')} | {_cell(r.model, '—')} | "
            f"{_cell(r.status, '—')} | {r.input_tokens} | {r.output_tokens} | "
            f"${r.cost_usd:.4f} |"
        )
    out = budget.take("\n".join(lines))
    if not out:
        return "Error: the context budget for this question is used up."

    # Newest first — a ticket retried three times has three failures, and the
    # last is the one being asked about. Added one at a time so a tight budget
    # keeps the most recent rather than truncating the oldest mid-line.
    for r in sorted(runs, key=lambda r: r.id, reverse=True):
        if not r.log_tail:
            continue
        heading = f"\n\n### Failed {_cell(r.phase)} run — transcript tail\n\n"
        # The fences are charged with the heading and sit *outside* the clip, so
        # a tail that gets truncated still ends in a closing fence rather than
        # leaving the rest of the prompt inside an unterminated code block.
        overhead = len(heading) + len(_FENCE_OPEN) + len(_FENCE_CLOSE)
        if budget.remaining < overhead + MIN_TAIL_CONTENT:
            break
        budget.used += overhead
        out += heading + _FENCE_OPEN + budget.take(r.log_tail, cap=LOG_TAIL_CAP) + _FENCE_CLOSE
    return out


def _get_resolver_status(args: dict, *, db: Session, user: User, budget: Budget, **_) -> str:
    """The resolver roster — **administrators only**.

    ``GET /resolvers`` is ``require_admin``. Handing every chat user the roster
    (bot usernames, env-file labels, models, effective config) would make the
    assistant a weaker path to admin-only data, which is precisely what the
    ticket gates exist to prevent. So the tool is exactly as permissive as the
    endpoint it wraps, and no more.
    """
    if not is_admin(user):
        return "Resolver status is available to administrators only."

    from schemas import ResolverSettingsValues  # local: avoids a router import cycle

    rows = db.query(ResolverInstance).order_by(ResolverInstance.bot_user_id.asc()).all()
    if not rows:
        return "No resolver has ever checked in."

    lines = ["| bot | name | agent | model | last seen | key settings |",
             "|---|---|---|---|---|---|"]
    for inst in rows:
        # Projected through the pydantic whitelist, never dumped raw:
        # `effective_config` is written by the resolver bot itself, so a buggy or
        # compromised one that heartbeats an extra key would otherwise leak it
        # here while the admin UI (which validates) would not.
        settings = ""
        if inst.effective_config:
            try:
                values = ResolverSettingsValues(**inst.effective_config)
                settings = (f"model={values.agent_model or '—'}, "
                            f"max_attempts={values.max_attempts}, "
                            f"verify={values.verify_command or '—'}")
            except Exception:
                settings = "(unreadable)"
        seen = inst.last_seen_at.isoformat() if inst.last_seen_at else "—"
        lines.append(
            f"| #{inst.bot_user_id} | {_cell(inst.name, '—')} | {_cell(inst.agent, '—')} | "
            f"{_cell(inst.model, '—')} | {seen} | {clip(_cell(settings, '—'), 200)} |"
        )
    return budget.take("\n".join(lines))


# --- The inert one -----------------------------------------------------------

def _propose_action(args: dict, *, db: Session, user: User, proposals: list, **_) -> str:
    """Record a suggestion for the user to confirm. **Executes nothing.**

    Routed through the same ``dispatch`` as the read tools, and writing to a
    caller-bound list, so the identity invariant covers it too and the loop never
    has to special-case a tool name.

    Everything in the payload originates with the model, which may in turn be
    echoing an instruction injected into a ticket description. So it is projected
    through a per-kind key whitelist and enum-validated here: a card that 422s
    when the user clicks Confirm is worse than no card at all.
    """
    if len(proposals) >= MAX_PROPOSALS:
        return f"Error: at most {MAX_PROPOSALS} actions may be proposed per answer."

    kind = _str(args, "kind", cap=40)
    if kind not in PROPOSAL_KINDS:
        return f"Error: `kind` must be one of: {', '.join(PROPOSAL_KINDS)}."

    raw = args.get("payload")
    if not isinstance(raw, dict):
        return "Error: `payload` must be an object."
    payload = {k: raw[k] for k in _PAYLOAD_KEYS[kind] if k in raw}

    try:
        payload = _clean_payload(kind, payload, db=db, user=user)
    except _BadArg as exc:
        return f"Error: {exc}"
    if payload is None:
        return NOT_FOUND

    proposals.append({
        "kind": kind,
        "payload": payload,
        "rationale": _str(args, "rationale", cap=RATIONALE_CAP),
    })
    # Short on purpose: a model proposing several things shouldn't spend its
    # remaining budget on acknowledgements.
    return "proposed"


def _clean_payload(kind: str, payload: dict, *, db: Session, user: User) -> dict | None:
    """Validate and normalize one proposal payload. ``None`` ⇒ unviewable ticket."""
    if "ticket_id" in payload:
        ticket_id = _int(payload, "ticket_id")
        if ticket_id is None:
            raise _BadArg("`ticket_id` is required.")
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        # Re-checked here, not just at Confirm: Confirm would 404 anyway, but a
        # card *naming* a ticket id is itself a disclosure that the id exists.
        if ticket is None or not can_view_ticket(user, ticket):
            return None
        payload["ticket_id"] = ticket_id

    if kind == "create_ticket":
        title = _str(payload, "title", cap=200)
        if not title:
            raise _BadArg("`title` is required for create_ticket.")
        payload["title"] = title
        payload["description"] = _str(payload, "description", cap=BODY_CAP)
        payload["type"] = _one_of(_str(payload, "type", cap=40) or "task", _TYPES, "type")
        payload["priority"] = _one_of(
            _str(payload, "priority", cap=40) or "medium", _PRIORITIES, "priority"
        )
        tags = payload.get("tags") or []
        if not isinstance(tags, list):
            raise _BadArg("`tags` must be a list of strings.")
        # Reserved tags are managed by the app and the resolver; the model cannot
        # know which, and including one would make the endpoint reject the whole
        # create. Drop them rather than fail the proposal.
        # Stripped, not just tested stripped: the endpoint normalizes on Confirm,
        # so an unstripped tag would show one thing on the card and create another.
        payload["tags"] = [
            stripped for stripped in (t.strip() for t in tags[:20] if isinstance(t, str))
            if stripped and not is_reserved_tag(stripped)
        ]
    elif kind == "add_comment":
        body = _str(payload, "body", cap=BODY_CAP)
        if not body:
            raise _BadArg("`body` is required for add_comment.")
        payload["body"] = body
    elif kind == "set_status":
        payload["status"] = _one_of(_str(payload, "status", cap=40), _STATUSES, "status")
    return payload


# --- Declarations and dispatch ----------------------------------------------

_HANDLERS = {
    "search_tickets": _search_tickets,
    "get_ticket": _get_ticket,
    "get_agent_runs": _get_agent_runs,
    "get_resolver_status": _get_resolver_status,
    "propose_action": _propose_action,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_tickets",
            "description": (
                "Find tickets the current user is allowed to see. Returns a "
                "compact table; use get_ticket for the full detail of one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring of the title."},
                    "status": {"type": "string", "enum": _STATUSES},
                    "tag": {"type": "string", "description": "One exact tag, e.g. repo:ticketing."},
                    "assigned_to_me": {"type": "boolean"},
                    "limit": {"type": "integer", "description": f"1-{SEARCH_LIMIT}, default 10."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticket",
            "description": (
                "Full context for one ticket: description, resolver agent runs, "
                "code blocks, comments and activity."
            ),
            "parameters": {
                "type": "object",
                "properties": {"ticket_id": {"type": "integer"}},
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_runs",
            "description": (
                "The resolver's per-phase runs on one ticket (plan/implement/"
                "review) with model, token usage, cost and status."
            ),
            "parameters": {
                "type": "object",
                "properties": {"ticket_id": {"type": "integer"}},
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resolver_status",
            "description": (
                "Which resolvers have checked in, what agent and model each runs, "
                "and when it was last seen. Administrators only."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": (
                "Suggest an action for the user to confirm. This does NOT perform "
                "it — a card appears and the user decides. Use it when the user "
                "asks for something to be done."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(PROPOSAL_KINDS)},
                    "payload": {
                        "type": "object",
                        "description": (
                            "create_ticket: type/title/description/priority/tags. "
                            "add_comment: ticket_id/body. request_fix: ticket_id. "
                            "set_status: ticket_id/status."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence on why, shown to the user on the card.",
                    },
                },
                "required": ["kind", "payload", "rationale"],
            },
        },
    },
]


def dispatch(name: str, args: dict, *, db: Session, user: User,
             proposals: list, budget: Budget) -> str:
    """Run one tool call and return its result as text for the model.

    ``args`` is untrusted; ``db``/``user``/``proposals``/``budget`` are bound by
    the caller and are **not reachable from it**. That separation is the whole
    security model — see the module docstring.

    Returns an error *string* for every failure. Nothing raises out of here: the
    caller is a generator producing an HTTP response body whose status line has
    already been sent, so an exception could only become a truncated stream,
    whereas a string lets the model notice and correct itself.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return (f"Error: no such tool `{name}`. Available: "
                f"{', '.join(sorted(_HANDLERS))}.")
    try:
        return handler(args, db=db, user=user, proposals=proposals, budget=budget)
    except _BadArg as exc:
        return f"Error: {exc}"
    except Exception:
        # Logged with a traceback for the operator; reported to the model as a
        # generic sentence. Exception text from SQLAlchemy carries table and
        # column names, and this string is about to be sent upstream.
        logger.exception("chat tool %r failed", name)
        return f"Error: `{name}` failed. Try a different approach."


def parse_arguments(raw: str) -> dict | None:
    """The model's ``arguments`` JSON as a dict, or ``None`` if it isn't one.

    Lives here rather than in the loop so that "what counts as valid arguments"
    is decided in the module that consumes them. An empty string means "no
    arguments", which is what a zero-parameter tool like ``get_resolver_status``
    legitimately sends.
    """
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
