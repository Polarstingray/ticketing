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
