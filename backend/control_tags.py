"""Reserved/control tags and who may manage them.

Tags are not purely cosmetic: the resolver bot uses a set of *control* tags as
workflow signals (mirrored here so the two definitions can't silently drift):

- ``claude:*`` — workflow state (planning, implementing, awaiting-pr-review,
  attempt counters) that drives whether the bot plans/implements/opens a PR.
- ``repo:<name>`` — which repository the resolver checks out and operates on.
- ``dangerous`` / ``fix`` — safety / behavior gates.

If an ordinary user could set these (via the UI or their own API key) on a
ticket they own, they could hijack the automation — point it at an arbitrary
repo, push a ticket into an "implement & open PR" phase, or strip the
``dangerous`` gate. So these tags may only be managed by a *trusted automation*
identity (the resolver bot) or an admin. Enforcement lives on the backend (the
trust boundary); a frontend-only restriction would be bypassable.

The bot is intentionally a non-admin ``member`` (least privilege), so it is
recognized here by user id via ``RESOLVER_BOT_USER_ID`` rather than by role.
"""
import os

from models import User, UserRole

# Reserved tag *prefixes* (any tag starting with one of these is reserved).
# `parent:<id>` links a delegated sub-task back to the ticket that spawned it, and
# `review-by:<id>` records who its finished PR is handed back to; both must be trusted
# so a user can't forge them to redirect another ticket's PR handoff. See the
# resolver's delegation flow.
RESERVED_PREFIXES = ("claude:", "repo:", "parent:", "review-by:")
# Reserved *exact* tags. `delegate` opts a ticket into resolver-to-resolver
# fan-out (the lead may decompose it and assign sub-tasks to other resolvers); like
# `dangerous` it must be trusted so a user can't self-trigger autonomous fan-out.
RESERVED_EXACT = frozenset({"dangerous", "fix", "delegate"})

# Trusted resolver bot user ids. Accepts a comma-separated list so multiple
# resolver identities (claude-bot, gemini-bot, open-bot, …) can all manage
# reserved control tags without being promoted to admin.
# e.g. RESOLVER_BOT_USER_ID=2,3,4  or the legacy single-id form: RESOLVER_BOT_USER_ID=2
_raw_bot_ids = os.environ.get("RESOLVER_BOT_USER_ID", "0")
RESOLVER_BOT_USER_IDS: frozenset[int] = frozenset(
    int(x.strip()) for x in _raw_bot_ids.split(",")
    if x.strip() and x.strip() != "0"
)


# --- Scoped API keys ---------------------------------------------------------
# A scope is a narrow, named capability carried by an *API key* rather than by its
# owning user, so it can be granted to a laptop CLI without promoting the human to
# admin. Only an admin may grant one (see routers/users.create_api_key): a member
# can mint their own keys, so self-service scoping would make this boundary
# decorative.
SCOPE_CLI = "cli"

# Which reserved tag prefixes each scope unlocks. `cli` gets `repo:` and nothing
# else: it names the checkout to review, and the resolver's PROJECTS_ROOT allowlist
# already bounds which checkouts exist. Deliberately excluded are the tags that
# would let a caller hijack the automation rather than merely aim it: `claude:*`
# (workflow phase), `dangerous`/`fix` (safety gates), `delegate` (autonomous
# fan-out), and `parent:`/`review-by:` (PR handoff routing).
SCOPE_TAG_PREFIXES: dict[str, tuple[str, ...]] = {SCOPE_CLI: ("repo:",)}

ALL_SCOPES = frozenset(SCOPE_TAG_PREFIXES)


def is_reserved_tag(tag: str) -> bool:
    """True if ``tag`` is a control tag that only trusted identities may set."""
    return tag in RESERVED_EXACT or tag.startswith(RESERVED_PREFIXES)


def reserved_subset(tags) -> set[str]:
    """The reserved tags within ``tags`` (order-insensitive)."""
    return {t for t in tags if is_reserved_tag(t)}


def can_manage_reserved_tags(user: User) -> bool:
    """Whether ``user`` may add/remove/alter reserved control tags.

    Admins always may; resolver bots may, so their state machines keep working
    without widening their role to admin. A bot is recognized by the DB flag
    ``is_resolver_bot`` (set at seed time — see ``seed.seed_resolver_bot``),
    which removes the old requirement that ``RESOLVER_BOT_USER_ID`` be kept in
    sync between the backend and the resolver. The legacy env-id list is still
    honored for backward compatibility with existing deployments.
    """
    if user.role == UserRole.admin.value:
        return True
    if getattr(user, "is_resolver_bot", False):
        return True
    return user.id in RESOLVER_BOT_USER_IDS


def can_set_tag(user: User, api_key, tag: str) -> bool:
    """Whether ``user``, authenticating with ``api_key``, may set ``tag``.

    Free tags are always allowed. Reserved tags need either a trusted identity
    (admin / resolver bot) or an API key whose scopes cover that tag's prefix.

    ``api_key`` is None for cookie sessions — a browser session carries the user's
    role, never a scope grant. That asymmetry is deliberate: the scope rides the
    key, so revoking the key revokes the capability.
    """
    if not is_reserved_tag(tag):
        return True
    if can_manage_reserved_tags(user):
        return True
    for scope in getattr(api_key, "scope_set", frozenset()):
        if tag.startswith(SCOPE_TAG_PREFIXES.get(scope, ())):
            return True
    return False


def unauthorized_tags(user: User, api_key, tags) -> set[str]:
    """The subset of ``tags`` this caller may not set (empty if all are allowed)."""
    return {t for t in tags if not can_set_tag(user, api_key, t)}
