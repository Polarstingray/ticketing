"""Read-only mode and demo-credential display, for the public Fly demo.

Neither of these exists for a normal self-hosted deployment: ``READ_ONLY``
defaults off (a self-host should behave like every other version of this app),
and ``SHOW_DEMO_CREDENTIALS`` is a *separate*, also-off-by-default flag —
turning on read-only mode must never, by itself, start publishing whatever
``ADMIN_PASSWORD`` a real deployment happens to have set. The two are only
both on together on the throwaway public demo, where the password is already
documented as intentionally public (see ``deploy/demo/Dockerfile``).

When shown, the credentials are read from ``ADMIN_USERNAME``/``ADMIN_PASSWORD``
— the *same* env vars ``seed.py``/``seed_demo.py`` already seed the admin
account from — rather than a second pair of env vars, so there is exactly one
place that can drift out of sync with what was actually seeded.
"""
import os
from dataclasses import dataclass
from functools import lru_cache

# Read once per process, like chat/config.py. Tests that need a different
# configuration call ``load.cache_clear()`` after patching the environment.


def _bool_env(name: str, default: bool) -> bool:
    """A boolean from the environment. Unset ⇒ ``default``; anything else is
    read leniently, since operators write ``1``, ``yes`` and ``on`` too.

    A local copy of chat/config.py's helper of the same name: three lines of
    env-parsing boilerplate isn't worth a shared module the way the security
    gates in ticket_queries.py are — there is no single invariant here that a
    second copy could drift out of sync with.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class DemoConfig:
    read_only: bool
    # None unless SHOW_DEMO_CREDENTIALS=true — never inferred from read_only.
    demo_username: str | None
    demo_password: str | None

    def public(self) -> dict:
        """The whole thing is already safe to hand to an unauthenticated
        browser — that's the point of this config — so there is nothing to
        redact here the way ChatConfig.public() redacts a URL and a key."""
        return {
            "read_only": self.read_only,
            "demo_username": self.demo_username,
            "demo_password": self.demo_password,
        }


@lru_cache(maxsize=1)
def load() -> DemoConfig:
    show_credentials = _bool_env("SHOW_DEMO_CREDENTIALS", False)
    return DemoConfig(
        read_only=_bool_env("READ_ONLY", False),
        demo_username=os.environ.get("ADMIN_USERNAME", "admin") if show_credentials else None,
        demo_password=os.environ.get("ADMIN_PASSWORD", "admin") if show_credentials else None,
    )
