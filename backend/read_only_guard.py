"""Blocks every mutating request when the deployment is read-only.

Exists for exactly one deployment — the public Fly demo — where a shared
`admin`/`demopass123` login is handed to strangers, and the point is that
nothing they do can touch ticket data, another visitor's work, resolver
settings, webhooks, or the admin account itself.

A middleware rather than ``Depends(require_not_read_only)`` sprinkled across
the ~30 write routes in ``routers/``: deny-by-default at the edge means a
*future* write route is protected for free, whereas a per-route dependency
only protects the routes someone remembered to annotate. The cost is that this
can't express per-route nuance — it can't, and shouldn't need to.

Two things are exempt, both because the demo needs them to be usable at all:

* ``/auth/login`` and ``/auth/logout`` — signing in is a POST, and a read-only
  demo nobody can sign into isn't a demo.
* Everything under ``/chat`` — asking the assistant a question persists a
  ``ChatMessage`` row, but that row is chat history, not ticket data, and
  answering questions is the feature being demoed. A *confirmed* proposed
  action is not exempt: ``ProposedAction.jsx`` calls the ordinary
  ``/tickets`` or ``/tickets/{id}/comments`` endpoints to execute it, so
  Confirm hits this same guard and surfaces the read-only message inline —
  no special-casing needed in the chat code for that to be true.
"""
from starlette.requests import Request
from starlette.responses import JSONResponse

import demo_config

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
EXEMPT_PATHS = {"/auth/login", "/auth/logout"}
EXEMPT_PREFIXES = ("/chat",)

MESSAGE = (
    "This is a read-only public demo, so nothing here writes. The chat "
    "assistant can still answer questions and propose an action, but "
    "confirming it hits the same block as any other change here."
)


def _is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES)


async def read_only_guard(request: Request, call_next):
    if (
        demo_config.load().read_only
        and request.method not in SAFE_METHODS
        and not _is_exempt(request.url.path)
    ):
        return JSONResponse(status_code=403, content={"detail": MESSAGE})
    return await call_next(request)
