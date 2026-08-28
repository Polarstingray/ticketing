"""Resolver settings routes — server-managed, non-secret resolver tunables.

An admin edits the resolver daemon's behavior (model routing, attempt limits,
verify gate, escalation, delegation) here instead of SSH-ing to change ``.env``.
The resolver fetches these at sweep start and overlays them on top of its
``.env`` defaults, so changes take effect on the *next sweep*.

Settings are keyed by ``bot_user_id`` (an optional query param) to support
multiple resolver identities; a ``NULL`` row is the global default used when a
resolver has no row of its own. GET is readable by any authenticated user; PUT
is admin-only. **Secrets are never accepted or returned** — provider keys stay
in ``.env`` and are surfaced as read-only descriptors (``SecretField``) so the
UI can show their presence without ever holding a value.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import get_api_key, get_current_user, require_admin
from control_tags import SCOPE_AGENT
from database import get_db
from models import AgentInstance, ResolverSettings, User, utcnow
from schemas import (
    AgentHeartbeat,
    AgentRosterEntry,
    ResolverHeartbeat,
    ResolverRosterEntry,
    ResolverSettingsOut,
    ResolverSettingsUpdate,
    ResolverSettingsValues,
    SecretField,
)

router = APIRouter(prefix="/resolver-settings", tags=["resolver-settings"])

# The live resolver registry (the manager roster + per-resolver heartbeat). A
# separate prefix from the settings above; both are wired in main.py.
registry_router = APIRouter(prefix="/resolvers", tags=["resolvers"])

# The agent-neutral view of that same registry. `/resolvers` is about *our*
# resolver bots and their settings; `/agents` is about every worker that has
# checked in, including third parties that have no resolver settings at all.
agents_router = APIRouter(prefix="/agents", tags=["agents"])

# Static, value-less descriptors: the secrets the resolver reads from its .env.
# Listed so the UI can render read-only "managed in .env" rows. The backend
# never holds these values, so there is nothing to leak.
SECRET_FIELDS = [
    SecretField(name="STINGRAY_API_KEY", label="Stingray API key"),
    SecretField(name="REVIEW_API_KEY", label="Review backend API key"),
    SecretField(name="CRITIQUE_API_KEY", label="Critique backend API key"),
    SecretField(name="AGENT_PROVIDER_KEYS", label="Agent provider keys (Anthropic / OpenAI / etc.)"),
]


def _row(db: Session, bot_user_id: int | None) -> ResolverSettings | None:
    return (
        db.query(ResolverSettings)
        .filter(ResolverSettings.bot_user_id == bot_user_id)
        .one_or_none()
    )


def _merged_values(db: Session, bot_user_id: int | None) -> ResolverSettingsValues:
    """Defaults <- global (NULL) row <- this bot's row. Later layers win, so a
    bot's explicit setting overrides the global default, which overrides the
    dataclass default baked into ResolverSettingsValues."""
    merged: dict = {}
    global_row = _row(db, None)
    if global_row and global_row.settings:
        merged.update(global_row.settings)
    if bot_user_id is not None:
        bot_row = _row(db, bot_user_id)
        if bot_row and bot_row.settings:
            merged.update(bot_row.settings)
    return ResolverSettingsValues(**merged)


def _out(db: Session, bot_user_id: int | None) -> ResolverSettingsOut:
    # updated_at/by come from the most specific row that actually exists.
    row = _row(db, bot_user_id) if bot_user_id is not None else None
    if row is None:
        row = _row(db, None)
    return ResolverSettingsOut(
        bot_user_id=bot_user_id,
        settings=_merged_values(db, bot_user_id),
        secrets=SECRET_FIELDS,
        updated_at=row.updated_at if row else None,
        updated_by=row.updated_by if row else None,
    )


@router.get("", response_model=ResolverSettingsOut)
def get_resolver_settings(
    bot_user_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _out(db, bot_user_id)


@router.put("", response_model=ResolverSettingsOut)
def update_resolver_settings(
    payload: ResolverSettingsUpdate,
    bot_user_id: int | None = None,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    # Only the fields the admin actually sent are persisted (partial update),
    # merged onto whatever is already stored for this identity.
    changes = payload.model_dump(exclude_unset=True, mode="json")
    row = _row(db, bot_user_id)
    if row is None:
        row = ResolverSettings(bot_user_id=bot_user_id, settings={})
        db.add(row)
    stored = dict(row.settings or {})
    stored.update(changes)
    row.settings = stored
    row.updated_by = admin.id
    # settings is a JSON column mutated in place above; reassigning ensures the
    # ORM flags it dirty. updated_at is refreshed by the model's onupdate.
    db.commit()
    return _out(db, bot_user_id)


# --- Live registry (resolver manager + agent registry) -----------------------

def _instance(db: Session, user_id: int) -> AgentInstance | None:
    """The live-registry row for one worker, if it has ever sent a heartbeat."""
    return (
        db.query(AgentInstance)
        .filter(AgentInstance.user_id == user_id)
        .one_or_none()
    )


def _upsert_instance(db: Session, user_id: int, fields: dict) -> AgentInstance:
    """Create-or-update this worker's registry row and stamp ``last_seen_at``."""
    def apply(inst: AgentInstance) -> None:
        for key, value in fields.items():
            setattr(inst, key, value)
        inst.last_seen_at = utcnow()

    inst = _instance(db, user_id)
    if inst is None:
        inst = AgentInstance(user_id=user_id)
        db.add(inst)
    apply(inst)
    try:
        db.commit()
    except IntegrityError:
        # Two overlapping heartbeats from the same worker both saw "no row" and
        # both inserted; the unique constraint on user_id rejects the loser. A
        # heartbeat is a plain upsert, so retry as an update instead of 500-ing.
        db.rollback()
        inst = _instance(db, user_id)
        if inst is None:
            raise
        apply(inst)
        db.commit()
    return inst


def _roster_entry(user: User, inst: AgentInstance | None, has_settings: bool) -> ResolverRosterEntry:
    effective = (
        ResolverSettingsValues(**inst.effective_config)
        if inst and inst.effective_config
        else None
    )
    return ResolverRosterEntry(
        bot_user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_bot=True,
        has_settings=has_settings,
        name=inst.name if inst else None,
        label=inst.label if inst else None,
        agent=inst.agent if inst else None,
        model=inst.model if inst else None,
        last_seen_at=inst.last_seen_at if inst else None,
        effective_config=effective,
    )


@registry_router.post("/heartbeat", response_model=ResolverRosterEntry)
def resolver_heartbeat(
    payload: ResolverHeartbeat,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """A resolver self-reports its identity + observed state each sweep. Posted
    by the resolver bot itself (its own API key); only resolver bots may call
    it. Upserts the instance row keyed by the caller's own user id and stamps
    ``last_seen_at``."""
    if not user.is_resolver_bot:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only resolver bots may send a heartbeat",
        )
    inst = _upsert_instance(db, user.id, {
        "label": payload.label,
        "name": payload.name,
        "agent": payload.agent,
        "model": payload.model,
        "effective_config": payload.effective_config.model_dump(mode="json"),
    })
    has_settings = _row(db, user.id) is not None
    return _roster_entry(user, inst, has_settings)


@registry_router.get("", response_model=list[ResolverRosterEntry])
def list_resolvers(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """The resolver-manager roster: every resolver-bot user, joined to its live
    self-reported state (null until it first sweeps) and whether it has any
    admin-authored settings override."""
    bots = (
        db.query(User)
        .filter(User.is_resolver_bot == True)  # noqa: E712
        .order_by(User.id.asc())
        .all()
    )
    instances = {i.user_id: i for i in db.query(AgentInstance).all()}
    settings_bots = {
        s.bot_user_id
        for s in db.query(ResolverSettings)
        .filter(ResolverSettings.bot_user_id.isnot(None))
        .all()
    }
    return [
        _roster_entry(bot, instances.get(bot.id), bot.id in settings_bots)
        for bot in bots
    ]


# --- Agent registry (agent-neutral alias) ------------------------------------

def _is_agent_caller(user: User, api_key) -> bool:
    """Whether this caller may register itself in the agent registry.

    Our own resolver bots always may (the identity is the credential). A third
    party may when its *API key* carries the ``agent`` scope — the scope rides
    the key, so revoking the key de-registers the worker, and a cookie session
    (``api_key`` is None) never qualifies.
    """
    if getattr(user, "is_resolver_bot", False):
        return True
    return SCOPE_AGENT in getattr(api_key, "scope_set", frozenset())


def _agent_entry(user: User, inst: AgentInstance | None) -> AgentRosterEntry:
    return AgentRosterEntry(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_resolver_bot=bool(user.is_resolver_bot),
        name=inst.name if inst else None,
        label=inst.label if inst else None,
        agent=inst.agent if inst else None,
        model=inst.model if inst else None,
        last_seen_at=inst.last_seen_at if inst else None,
    )


@agents_router.post("/heartbeat", response_model=AgentRosterEntry)
def agent_heartbeat(
    payload: AgentHeartbeat,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    api_key=Depends(get_api_key),
):
    """A worker self-reports its identity + liveness. The agent-neutral twin of
    ``POST /resolvers/heartbeat``: same row, but open to third-party agents
    holding an ``agent``-scoped key rather than to resolver bots only. Upserts
    the row keyed by the caller's own user id and stamps ``last_seen_at``."""
    if not _is_agent_caller(user, api_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An agent-scoped API key is required to send a heartbeat",
        )
    inst = _upsert_instance(db, user.id, {
        "label": payload.label,
        "name": payload.name,
        "agent": payload.agent,
        "model": payload.model,
        "effective_config": payload.effective_config,
    })
    return _agent_entry(user, inst)


@agents_router.get("", response_model=list[AgentRosterEntry])
def list_agents(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Every worker that has checked in, ours and third-party alike, so an
    operator can see who is live and when each was last seen. Unlike
    ``GET /resolvers`` this is driven by the registry rows rather than by the
    resolver-bot flag, so an external agent appears here the moment it first
    heartbeats and never appears in the resolver settings roster."""
    instances = (
        db.query(AgentInstance).order_by(AgentInstance.user_id.asc()).all()
    )
    if not instances:
        return []
    users = {
        u.id: u
        for u in db.query(User).filter(User.id.in_([i.user_id for i in instances])).all()
    }
    # A row whose user has since been deleted has no identity to show, so it is
    # skipped rather than rendered as a blank.
    return [_agent_entry(users[i.user_id], i) for i in instances if i.user_id in users]
