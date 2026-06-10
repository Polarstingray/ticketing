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
RESERVED_PREFIXES = ("claude:", "repo:")
# Reserved *exact* tags.
RESERVED_EXACT = frozenset({"dangerous", "fix"})

# The resolver bot's user id. Set this in the environment (memory: the seeded
# claude-bot is typically user id 2) so the bot keeps the ability to transition
# control tags; 0 means "unset", and then only admins can manage reserved tags.
RESOLVER_BOT_USER_ID = int(os.environ.get("RESOLVER_BOT_USER_ID", "0"))


def is_reserved_tag(tag: str) -> bool:
    """True if ``tag`` is a control tag that only trusted identities may set."""
    return tag in RESERVED_EXACT or tag.startswith(RESERVED_PREFIXES)


def reserved_subset(tags) -> set[str]:
    """The reserved tags within ``tags`` (order-insensitive)."""
    return {t for t in tags if is_reserved_tag(t)}


def can_manage_reserved_tags(user: User) -> bool:
    """Whether ``user`` may add/remove/alter reserved control tags.

    Admins always may; the resolver bot may (matched by id) so its state
    machine keeps working without widening its role to admin.
    """
    if user.role == UserRole.admin.value:
        return True
    return RESOLVER_BOT_USER_ID != 0 and user.id == RESOLVER_BOT_USER_ID
