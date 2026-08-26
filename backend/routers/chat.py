"""Chat assistant routes — ask a question about a ticket.

Two endpoints in this phase: a capability probe the SPA calls on load, and a
single question-and-answer turn. There is no conversation state yet (the next
phase adds it, along with streaming) and there are no tools, so this router can
only ever read one ticket the caller already has access to and forward it to the
configured model.

Three boundaries are worth stating explicitly, because they are what make the
feature safe to add rather than a new attack surface:

* **Authorization is delegated, not reimplemented.** ``chat.context.ticket_pack``
  goes through ``can_view_ticket``; a ticket the caller may not see returns 404,
  the same probe-resistant answer ``GET /tickets/{id}`` gives.
* **No writes.** The assistant has no path to modify anything. Ticket text is
  attacker-controllable, so the mitigation for prompt injection is structural —
  there is nothing here for an injected instruction to reach.
* **Metered calls are rate limited.** Each request costs the operator money at a
  third-party provider, so it carries a per-IP budget like ``POST /auth/login``.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from auth import get_current_user
from chat import config as chat_config
from chat import context as chat_context
from chat import prompts, provider
from chat.budget import estimate_cost
from database import get_db
from models import User
from ratelimit import limiter
from schemas import ChatAskRequest, ChatAskResponse, ChatConfigOut, ChatUsage

router = APIRouter(prefix="/chat", tags=["chat"])

# Read at import time, like LOGIN_RATE_LIMIT: slowapi resolves the decorator's
# argument once when the route is registered.
_RATE_LIMIT = chat_config.load().rate_limit


@router.get("/config", response_model=ChatConfigOut)
def get_chat_config(user: User = Depends(get_current_user)):
    """Whether the assistant is available, and which model answers.

    Authenticated rather than public: an unauthenticated caller has no use for
    it, and it needn't advertise the deployment's model to the internet.
    """
    return ChatConfigOut(**chat_config.load().public())


@router.post("/ask", response_model=ChatAskResponse)
@limiter.limit(_RATE_LIMIT)
def ask(
    request: Request,  # required by slowapi's limiter, like POST /auth/login
    payload: ChatAskRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cfg = chat_config.load()
    if not cfg.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The chat assistant is not configured on this deployment "
                "(set CHAT_API_URL, CHAT_API_KEY and CHAT_API_MODEL)."
            ),
        )

    pack = None
    if payload.ticket_id is not None:
        pack = chat_context.ticket_pack(
            db, user, payload.ticket_id, budget=cfg.context_budget
        )
        if pack is None:
            # 404 for both "no such ticket" and "not yours" — see get_ticket.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found"
            )

    try:
        completion = provider.complete(
            cfg,
            prompts.SYSTEM_PROMPT,
            prompts.build_user_message(payload.question, pack),
        )
    except provider.ProviderError as exc:
        # ProviderError carries the status the upstream failure maps to, and its
        # message is already written for a human to read in the chat panel.
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None

    return ChatAskResponse(
        answer=completion.text,
        usage=ChatUsage(
            model=completion.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_usd=estimate_cost(
                completion.input_tokens,
                completion.output_tokens,
                cfg.price_in_per_mtok,
                cfg.price_out_per_mtok,
            ),
        ),
        context_ticket_id=payload.ticket_id if pack else None,
        context_chars=len(pack or ""),
    )
