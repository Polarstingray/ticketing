"""Chat assistant routes — configuration, conversations, and streamed answers.

The assistant reads only what the calling user may already read, and performs
no writes. Four boundaries make it safe to add rather than a new attack surface:

* **Authorization is delegated, not reimplemented.** ``chat.context.ticket_pack``
  goes through ``can_view_ticket``; a ticket the caller may not see yields 404,
  the same probe-resistant answer ``GET /tickets/{id}`` gives. A conversation's
  stored ``ticket_id`` is re-resolved this way on *every* turn, so it is an anchor
  and never a stored grant.
* **Conversations are strictly per-owner — including from admins.** Unlike
  tickets, there is no admin override: a thread quotes ticket content, and a
  second, weaker path to that data is not worth having.
* **Tools are read-only and caller-bound.** ``chat/tools.py`` dispatches with
  ``db``/``user`` bound by *this* module via ``functools.partial``, never from
  the model's arguments; every tool re-derives visibility from that user. The
  assistant can *propose* an action, which renders a card the user confirms — the
  confirmation calls the existing endpoints as the user. There is no write path
  here at all, which is what makes injected ticket text a nuisance and not a
  vulnerability.
* **Metered calls are bounded twice.** A per-IP rate limit caps bursts; a
  per-user daily USD cap (``chat/spend.py``) caps the bill. A turn is now up to
  ``CHAT_MAX_TOOL_HOPS + 1`` provider calls, so the loop also stops escalating
  once the running cost crosses that cap.

Answers stream as Server-Sent Events. ``EventSource`` cannot issue a POST or
carry a body, so the browser reads the stream with ``fetch`` + a reader instead
(see ``frontend/src/api.js``).
"""
import functools
import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from auth import get_current_user
from chat import config as chat_config
from chat import context as chat_context
from chat import loop as chat_loop
from chat import prompts, provider, spend
from chat import summarize as chat_summarize
from chat import tools as chat_tools
from chat.budget import Budget, estimate_cost
from database import SessionLocal, get_db
from models import ChatConversation, ChatMessage, ChatRole, User, utcnow
from ratelimit import limiter
from schemas import (
    ChatAskRequest,
    ChatAskResponse,
    ChatConfigOut,
    ChatConversationCreate,
    ChatConversationOut,
    ChatConversationSummary,
    ChatSendRequest,
    ChatUsage,
)

router = APIRouter(prefix="/chat", tags=["chat"])

# Read at import time, like LOGIN_RATE_LIMIT: slowapi resolves the decorator's
# argument once when the route is registered.
_RATE_LIMIT = chat_config.load().rate_limit

# How many threads the popup's list shows. Threads are cheap and never expire, so
# the list is bounded rather than paginated — the popup is not an archive browser.
MAX_CONVERSATIONS = 50


def _require_enabled(cfg) -> None:
    if not cfg.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The chat assistant is not configured on this deployment "
                "(set CHAT_API_URL, CHAT_API_KEY and CHAT_API_MODEL)."
            ),
        )


def _owned_or_404(db: Session, user: User, conversation_id: int) -> ChatConversation:
    """The caller's conversation, or 404.

    404 rather than 403 for someone else's thread, and no admin override — see
    the module docstring.
    """
    convo = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == user.id,
        )
        .one_or_none()
    )
    if convo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return convo


def _pack_or_404(db: Session, user: User, ticket_id: int | None, cfg) -> str | None:
    """Context for ``ticket_id`` under this caller's permissions, or None if no
    ticket was asked for. Raises 404 when the ticket is unreadable or absent."""
    if ticket_id is None:
        return None
    pack = chat_context.ticket_pack(db, user, ticket_id, budget=cfg.context_budget)
    if pack is None:
        # 404 for both "no such ticket" and "not yours" — see get_ticket.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found"
        )
    return pack


def _check_budget(db: Session, user: User, cfg) -> None:
    try:
        spend.check_daily_cap(db, user.id, cfg.daily_usd_limit)
    except spend.DailyCapExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from None


# --- Configuration -----------------------------------------------------------

@router.get("/config", response_model=ChatConfigOut)
def get_chat_config(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Whether the assistant is available, which model answers, and what this
    caller has spent today.

    Authenticated rather than public: an unauthenticated caller has no use for
    it, and it needn't advertise the deployment's model to the internet.
    """
    cfg = chat_config.load()
    return ChatConfigOut(
        **cfg.public(),
        # Reported even when no cap is configured, so the popup can show a
        # running total without a second endpoint.
        spent_today_usd=spend.spent_today(db, user.id) if cfg.enabled else 0.0,
    )


# --- Conversations -----------------------------------------------------------

@router.get("/conversations", response_model=list[ChatConversationSummary])
def list_conversations(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """This caller's threads, most recently active first. Never anyone else's."""
    return (
        db.query(ChatConversation)
        .filter(ChatConversation.user_id == user.id)
        .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
        .limit(MAX_CONVERSATIONS)
        .all()
    )


@router.post("/conversations", response_model=ChatConversationOut,
             status_code=status.HTTP_201_CREATED)
def create_conversation(
    payload: ChatConversationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cfg = chat_config.load()
    _require_enabled(cfg)
    # Validate the anchor now so an unreadable ticket fails at creation rather
    # than on the first question — even though every turn re-checks it anyway.
    if payload.ticket_id is not None:
        _pack_or_404(db, user, payload.ticket_id, cfg)

    convo = ChatConversation(user_id=user.id, ticket_id=payload.ticket_id, title="")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return convo


@router.get("/conversations/{conversation_id}", response_model=ChatConversationOut)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    convo = _owned_or_404(db, user, conversation_id)
    convo.messages.sort(key=lambda m: (m.created_at, m.id))
    return convo


@router.delete("/conversations/{conversation_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a thread and its messages.

    The cascade matters: a thread is the only place quoted ticket content
    persists, so deleting it is how a user makes that copy go away.
    """
    convo = _owned_or_404(db, user, conversation_id)
    db.delete(convo)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Streaming a turn --------------------------------------------------------

def _sse(event: str, data: dict) -> str:
    """One Server-Sent Event frame.

    ``json.dumps`` guarantees the payload is single-line, which the SSE framing
    requires — an unescaped newline inside `data:` would split one event into two.
    """
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _history(convo: ChatConversation, turns: int) -> list[tuple[str, str]]:
    """The last ``turns`` exchanges of a thread, oldest-first.

    Counted in messages rather than exchanges, then trimmed from the front: a
    thread whose first turn failed can have an odd number of messages, and
    starting the replay on an assistant turn is harmless where dropping the most
    recent question would not be.
    """
    ordered = sorted(convo.messages, key=lambda m: (m.created_at, m.id))
    recent = ordered[-(turns * 2):] if turns > 0 else []
    return [(m.role, m.content) for m in recent if m.content]


def _stream_turn(conversation_id: int, user_id: int, question: str,
                 pack: str | None, ticket_id: int | None, cfg) -> Iterator[str]:
    """Generate the SSE frames for one turn, persisting both messages around it.

    This opens its **own** database session. The request-scoped session from
    ``get_db`` is closed when the endpoint returns, which for a StreamingResponse
    is *before* the body has been produced — so writing the assistant's turn
    through it would use a closed session.

    The user's message is written first and committed, so a question is recorded
    even if the provider then fails. The assistant's message is written only when
    there is text to store; a stream that died before its first token leaves the
    question standing alone, which is what actually happened.

    The ``User`` the tools enforce against is **re-loaded here**, in this session,
    rather than passed in from the request. The request-scoped instance would be
    detached by now, and handing a tool a detached ORM object is the kind of bug
    that passes in tests and fails against a real connection pool. Re-reading it
    also means the identity the tools check is the one this session sees.
    """
    db = SessionLocal()
    try:
        convo = (
            db.query(ChatConversation)
            .filter(ChatConversation.id == conversation_id)
            .one_or_none()
        )
        if convo is None:  # deleted between the request and the stream starting
            yield _sse("error", {"detail": "Conversation not found"})
            return
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if user is None:  # deleted mid-flight; nothing to enforce against
            yield _sse("error", {"detail": "Conversation not found"})
            return
        # What this user had already spent when the turn began. The loop adds the
        # turn's own running cost to it — `spent_today` only counts *persisted*
        # turns, so re-querying it between hops would return the same number.
        spent_before = spend.spent_today(db, user_id)

        history = _history(convo, cfg.history_turns)
        history = chat_summarize.maybe_summarize(
            history, cfg,
            threshold=cfg.summary_threshold,
            keep_turns=cfg.summary_keep_turns,
        )
        db.add(ChatMessage(
            conversation_id=convo.id,
            role=ChatRole.user.value,
            content=question,
            meta={"ticket_id": ticket_id, "context_chars": len(pack or "")},
        ))
        if not convo.title:
            convo.title = prompts.derive_title(question)
        convo.updated_at = utcnow()
        db.commit()

        state = chat_loop.TurnState()
        # Bound here, in the stream-local session, and never from the model's
        # arguments: `dispatch` receives a name and an untrusted dict, and gets
        # its identity and its database from this partial. See chat/tools.py.
        budget = Budget(cfg.context_budget)
        do_tool = functools.partial(
            chat_tools.dispatch, db=db, user=user,
            proposals=state.proposed_actions, budget=budget,
        )
        try:
            for event, data in chat_loop.run(
                cfg,
                prompts.SYSTEM_PROMPT,
                prompts.build_messages(history, question, pack),
                state,
                dispatch=do_tool,
                tools=chat_tools.TOOLS,
                budget=budget,
                spent_before=spent_before,
            ):
                yield _sse(event, data)
        except provider.ProviderError as exc:
            # Pre-stream failures only; a mid-stream fault ends the iteration
            # instead, leaving the partial text in `state`.
            yield _sse("error", {"detail": str(exc), "status": exc.status})
            return

        # A turn that only proposed an action and said nothing is degenerate, but
        # it is not a provider failure and must not be reported as one.
        if not state.text and not state.proposed_actions:
            yield _sse("error", {"detail": "The model returned an empty response."})
            return

        cost = estimate_cost(
            state.input_tokens, state.output_tokens,
            cfg.price_in_per_mtok, cfg.price_out_per_mtok,
        )
        # Tool keys are added only when non-empty, so a plain turn's meta stays
        # exactly what it has always been.
        meta = {"ticket_id": ticket_id, "context_chars": len(pack or "")}
        if state.tool_calls:
            meta["tool_calls"] = state.tool_calls
        if state.proposed_actions:
            meta["proposed_actions"] = state.proposed_actions
        if state.capped:
            meta["tool_hops_capped"] = True
        message = ChatMessage(
            conversation_id=convo.id,
            role=ChatRole.assistant.value,
            content=state.text,
            model=state.model,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            cost_usd=cost,
            meta=meta,
        )
        db.add(message)
        convo.updated_at = utcnow()
        db.commit()
        db.refresh(message)

        yield _sse("done", {
            "message_id": message.id,
            "conversation_id": convo.id,
            "title": convo.title,
            "usage": {
                "model": state.model,
                "input_tokens": state.input_tokens,
                "output_tokens": state.output_tokens,
                "cost_usd": cost,
            },
            # The *same blob that was stored*, so a turn rendered live and the
            # same turn after a reload have identical shape in the frontend.
            "meta": meta,
            "spent_today_usd": spend.spent_today(db, user_id),
        })
    finally:
        db.close()


@router.post("/conversations/{conversation_id}/messages")
@limiter.limit(_RATE_LIMIT)
def send_message(
    request: Request,  # required by slowapi's limiter, like POST /auth/login
    conversation_id: int,
    payload: ChatSendRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Ask a question in a thread; the answer streams back as SSE.

    Every gate — ownership, the ticket's readability, the daily budget — is
    checked here, synchronously, *before* the response starts. Once the stream is
    open the status line is already sent, so a refusal at that point could only be
    an error frame in a 200 response; failing early keeps real HTTP statuses.
    """
    cfg = chat_config.load()
    _require_enabled(cfg)
    convo = _owned_or_404(db, user, conversation_id)
    _check_budget(db, user, cfg)

    # The turn's ticket overrides the thread's anchor, and is re-resolved against
    # this caller's permissions every time — a stored anchor grants nothing.
    ticket_id = payload.ticket_id if payload.ticket_id is not None else convo.ticket_id
    pack = _pack_or_404(db, user, ticket_id, cfg)

    return StreamingResponse(
        _stream_turn(convo.id, user.id, payload.content, pack, ticket_id, cfg),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx buffers proxied responses by default, which would hold the
            # whole answer until it completed and defeat streaming entirely.
            "X-Accel-Buffering": "no",
        },
    )


# --- Stateless one-shot ------------------------------------------------------

@router.post("/ask", response_model=ChatAskResponse)
@limiter.limit(_RATE_LIMIT)
def ask(
    request: Request,  # required by slowapi's limiter, like POST /auth/login
    payload: ChatAskRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One question, one answer, no thread — for API clients and scripts.

    **No tools.** This path stays a single completion: ``ChatAskResponse`` has
    nowhere to put tool records or proposed actions, and growing a second tool
    parser for a non-streaming shape would guarantee the two drift. If parity is
    ever wanted, the move is to implement ``complete()`` on top of ``stream()``,
    not to duplicate the accumulation.

    Kept alongside the conversation endpoints rather than replaced by them: it is
    the whole surface a non-browser caller needs, and it does not stream, so it
    needs no SSE parser on the other end. Its turns are not stored, and so do not
    count toward the daily cap — the cap is enforced on it, but only from spend
    the conversation endpoints recorded.
    """
    cfg = chat_config.load()
    _require_enabled(cfg)
    _check_budget(db, user, cfg)
    pack = _pack_or_404(db, user, payload.ticket_id, cfg)

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
