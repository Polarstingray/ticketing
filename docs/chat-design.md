# The chat assistant: design notes

An in-app AI chat popup that answers questions about **ticket context** and helps
**debug resolver runs**. Like [`resolver-design.md`](./resolver-design.md), this doc
records the decisions and the tradeoffs, not the line-by-line code — but every
signature and file path below is real, and the plan at the end is the build order.

> TL;DR — a floating popup available on every authed page. The backend assembles a
> permission-scoped *context pack* from the tickets the caller can already see, runs a
> bounded read-only tool loop against an OpenAI-compatible endpoint, and streams the
> answer back over SSE. It can **propose** actions (file a ticket, post a comment,
> request `/fix`) as cards the user clicks to confirm — it never writes on its own.
> Every turn is persisted with its model, tokens and USD cost, the same way agent runs
> are.

## Design goals

1. **The read boundary is the model's boundary.** The assistant can see exactly what
   the calling user can see — nothing more. `_visible_tickets` and `can_view_ticket`
   are the only gates, reused verbatim. The model can name a *ticket id*; it can never
   name an *identity*.
2. **No side effects without a human click.** Ticket descriptions, comments and
   resolver output are attacker-controllable text. Anything with a write is *proposed*
   and executed by the browser through the existing, already-tested endpoints.
3. **Visible and costed.** Same principle as the resolver: invisible AI spend is a
   product bug. Every assistant turn records model + tokens + USD, and the popup shows
   the running total.
4. **Provider-agnostic, and cleanly absent.** Configured like the resolver's
   `REVIEW_API_*` trio. With nothing configured the feature reports itself disabled and
   the launcher never renders — the core app is unchanged.
5. **Debugging the resolver is a first-class use case**, not a side effect. That takes
   one new fact the server doesn't have today: why a failed run failed.

## What it can actually see

Today the app stores agent-run **metadata** (`AgentRun`: agent, phase, model, tokens,
cost, status) but the plan/implement transcripts are files on the dev station
(`resolver/logs/ticket-<id>-<phase>-<ts>.log`), invisible to the server. So "why did
implement fail on #42" is unanswerable from the database alone.

The fix is small and bounded: **failed runs ship a truncated log tail with the run**.

- `models.AgentRun` gains `log_tail = Column(Text, nullable=False, default="")`.
- `schemas.AgentRunCreate` gains `log_tail: str = Field(default="", max_length=20000)`;
  `AgentRunOut` exposes it (already gated by `can_view_ticket` in `list_agent_runs`,
  `backend/routers/tickets.py:484`).
- Resolver side: at each `client.create_agent_run(...)` call site (the plan said
  `add_agent_run`; the method is `create_agent_run`), pass
  `log_tail=failed_log_tail(log_path, ok)` — empty unless the run failed.
- **Redaction reuses `audit.redact`, which already existed.** The plan called for a
  new `_redact`; writing one would have been a second, weaker copy of the scrubber
  every log line already goes through. What *was* missing is that only `cfg.api_key`
  was registered as a literal secret — `review_api_key`, `critique_api_key` and the
  digest keys were not. The regexes catch token *shapes* (`gh*_`, `sk_`, `Bearer`),
  so a provider key shaped like none of them was scrubbed nowhere.
  `audit.setup_logging` now registers all of them. That gap mattered little while
  the tail stayed on the dev station and matters a great deal now that it is
  persisted in the app. An agent transcript is a plausible place for a key to land
  in an echoed command; this is the one place to catch it.
- **Only failure ships a tail**, enforced twice: `failed_log_tail` returns `""` for a
  successful run, and `create_agent_run` drops a tail sent alongside
  `status="succeeded"` — the sender is an unattended bot, so this is enforced rather
  than trusted.

That single field is what turns "run 3 failed" into "run 3 failed because the verify
command couldn't find `.venv` in a fresh worktree."

## Data model

Two new tables, following the conventions already in `models.py` — sparse rows, JSON
blob for the open-ended part (like `Ticket.code_blocks` / `ResolverSettings.settings`),
strict per-user scoping (like `Notification`).

```python
class ChatConversation(Base):
    """One chat thread, owned by exactly one user.

    Strictly per-user: unlike tickets there is no admin override, because a thread
    embeds quoted ticket content the owner could see *at the time*. `ticket_id` is
    the ticket the thread was opened from — an anchor for the context pack, not a
    hard scope; the tool loop may pull in other tickets the owner can see.
    """
    __tablename__ = "chat_conversations"
    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ticket_id   = Column(Integer, ForeignKey("tickets.id"), nullable=True, index=True)
    title       = Column(String, nullable=False, default="")   # derived from turn 1
    created_at  = Column(DateTime, default=utcnow, nullable=False)
    updated_at  = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    messages    = relationship("ChatMessage", cascade="all, delete-orphan")


class ChatMessage(Base):
    """One turn. Assistant turns carry their own token/cost accounting, mirroring
    AgentRun — the same "AI work is visible and costed" rule, applied to chat.

    `meta` holds the open-ended per-turn extras as JSON: tool calls made, tool
    results (truncated), proposed actions, and the ticket ids that went into the
    context pack. Kept as a blob so a new tool needs no migration.
    """
    __tablename__ = "chat_messages"
    id              = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id"),
                             nullable=False, index=True)
    role            = Column(String, nullable=False)   # user | assistant
    content         = Column(Text, nullable=False, default="")
    model           = Column(String, nullable=False, default="")
    input_tokens    = Column(Integer, nullable=False, default=0)
    output_tokens   = Column(Integer, nullable=False, default=0)
    cost_usd        = Column(Float, nullable=False, default=0.0)
    meta            = Column(JSON, nullable=False, default=dict)
    created_at      = Column(DateTime, default=utcnow, nullable=False)
```

**Tool hops are not persisted.** Only the question and the final answer become
rows; the calls and proposals of a multi-hop turn live in `meta`. A stored
`role: "tool"` message would be replayed by `_history()`, whose trimming window
is deliberately arbitrary — once a thread grows past `history_turns` the trim can
land between an assistant message and the tool results it refers to, and an
orphan tool message is a hard 400 from the provider. Keeping hops in memory also
keeps one cost per turn, which is what `spend.spent_today` sums.


Migrations (`backend/migrations.py`, appended to `MIGRATIONS` in order):

- `_migrate_agent_run_log_tail` — `_add_column(engine, "agent_runs", "log_tail", "TEXT NOT NULL DEFAULT ''")`
- `_migrate_chat_tables` — explicit idempotent `create_all(..., tables=[...], checkfirst=True)`,
  same shape as `_migrate_notification_preferences`.

## Context assembly

New package `backend/chat/`:

| Module | Responsibility | Phase |
|---|---|---|
| `config.py` | Env-driven provider settings; the feature's on/off switch | 1 ✅ |
| `context.py` | Build the permission-scoped context pack | 1 ✅ |
| `budget.py` | Character budget for the pack; cost estimation | 1 ✅ |
| `spend.py` | Per-user daily USD cap (a query, so kept out of `budget.py`) | 2 ✅ |
| `prompts.py` | System prompt and the untrusted-context fence | 1 ✅ |
| `provider.py` | OpenAI-compatible chat completion, streaming | 1–2 ✅ |
| `tools.py` | The read-only tool schema + dispatch, bound to `(db, user)` | 3 |

The daily USD cap landed in phase 2 as `chat/spend.py` rather than inside `budget.py`:
the cap is a *query* (it sums persisted turn costs), and `budget.py` is pure arithmetic
with no database imports, which is what makes it trivially testable.

```python
# backend/chat/context.py
def ticket_pack(db: Session, user: User, ticket_id: int, *, budget: int) -> str | None:
    """Markdown context for one ticket, or None if `user` may not view it.

    Goes through can_view_ticket — the same 404-not-403 boundary get_ticket uses,
    so the pack can't confirm the existence of a ticket the caller can't see.
    Sections, trimmed oldest-first to fit `budget` characters:
      header (id/type/status/priority/assignee/tags/dates) — never trimmed
      description · code_blocks (per-block cap) · comments · activity
      agent runs, including log_tail for failed runs
    """
```

Budget defaults to ~60k characters (≈15k tokens), enforced in `budget.py` rather than
guessed at the call site, so a ticket with fifty comments and a 3k-line code block
degrades predictably instead of 400-ing at the provider.

## The tool loop

Four read-only tools, declared to the provider as OpenAI `tools`. Dispatch is
`functools.partial`-bound to the request's `(db, user)`:

```python
# backend/chat/tools.py
TOOLS = [search_tickets, get_ticket, get_agent_runs, get_resolver_status]

def dispatch(name: str, args: dict, *, db: Session, user: User) -> str:
    """Run one tool call. `args` is model-supplied and untrusted; `db`/`user` are
    bound by the caller and are NOT reachable from `args`. That separation is the
    whole security model: the model chooses *what to ask about*, never *who is
    asking*."""
```

- `search_tickets(query?, status?, tag?, assigned_to_me?, limit<=20)` — starts from
  `_visible_tickets(db, user)`, reusing `_tag_clause` for tag matching.
- `get_ticket(ticket_id)` → `ticket_pack(...)`.
- `get_agent_runs(ticket_id)` → runs, plus each failed run's redacted transcript
  tail, newest first (phase 4).
- `get_resolver_status()` → the `ResolverInstance` roster and non-secret
  `effective_config`, so "is the gemini resolver even running?" is answerable.

The loop is capped at `CHAT_MAX_TOOL_HOPS` (default 6). Exceeding it ends the turn with
a plain message rather than an error.

## Proposed actions, not writes

The assistant has one more "tool" that is deliberately inert:

```python
def propose_action(kind: str, payload: dict, rationale: str) -> str:
    """Record a suggested action for the USER to confirm. Executes nothing.
    Returns "proposed"; the payload lands in ChatMessage.meta["proposed_actions"]."""
```

`kind` ∈ `create_ticket` · `add_comment` · `request_fix` · `set_status`. The frontend
renders each as a card with a Confirm button that calls the **existing** endpoint —
`api.createTicket`, `api.addComment`, the `/fix` path `TicketDetail.jsx` already has —
as the logged-in user.

This is the load-bearing decision. It means: no new write endpoints, no new
authorization code, no new audit path (the existing `activity.py` entry is written by
the existing route), and a prompt injected into a ticket description can at worst make
a button *appear* that the user has to read and click. The write surface of this
feature is zero.

## Streaming

`POST /chat/conversations/{id}/messages` returns `text/event-stream` via
`StreamingResponse`. Events:

```
event: token        data: {"text": "..."}          # incremental content
event: tool_call    data: {"name": "get_ticket", "args": {...}}
event: tool_result  data: {"name": "get_ticket", "summary": "1 ticket, 4.2k chars"}
event: done         data: {"message_id": 91, "usage": {...}, "cost_usd": 0.0021}
event: error        data: {"detail": "..."}
```

`EventSource` is GET-only and can't carry a body, so the client uses
`fetch` + `res.body.getReader()`. `api.js` grows one function alongside `request`:

```js
// Streams an SSE response, invoking onEvent({event, data}) per frame.
// Same credentials/BASE conventions as request(); the only non-JSON path in this module.
async function stream(path, body, onEvent, { signal } = {}) { ... }
```

`provider.py` uses `httpx` (synchronous, native streaming) — a **new backend dependency**;
`backend/requirements.txt` currently has no HTTP client at all. The resolver's
`_chat_completion` (`resolver/resolve_tickets.py:2249`) is the non-streaming ancestor of
this code and is worth reading first; the response parsing and the 429-means-quota
handling carry over directly.

## Configuration

Mirrors the resolver's `REVIEW_API_*` trio, read from the backend's environment. **No
secrets in the database** — the same rule `ResolverSettings` documents.

| Var | Meaning |
|---|---|
| `CHAT_API_URL` | OpenAI-compatible `/chat/completions` endpoint |
| `CHAT_API_KEY` | Bearer key |
| `CHAT_API_MODEL` | Model id |
| `CHAT_MAX_TOOL_HOPS` | Tool-loop cap (default 6) |
| `CHAT_TIMEOUT` | Per-request seconds (default 120) |
| `CHAT_RATE_LIMIT` | slowapi budget on send (default `20/minute`) |
| `CHAT_DAILY_USD_LIMIT` | Per-user daily cap; `0` = off |
| `CHAT_PRICE_IN` / `CHAT_PRICE_OUT` | USD per 1M tokens, for the cost column |

`GET /chat/config` → `{"enabled": bool, "model": str, "daily_usd_limit": float,
"spent_today_usd": float}`. All three of URL/KEY/MODEL must be set for `enabled` — the
same all-or-nothing check as `_single_shot_enabled` (`resolve_tickets.py:2241`). The
frontend hides the launcher entirely when disabled, so a stock deployment sees no trace
of the feature.

## API surface

```
GET    /chat/config
GET    /chat/conversations                 -> [{id, title, ticket_id, updated_at}]
POST   /chat/conversations                 -> {id}            body: {ticket_id?}
GET    /chat/conversations/{id}            -> {..., messages: [...]}
DELETE /chat/conversations/{id}
POST   /chat/conversations/{id}/messages   -> SSE stream      body: {content, ticket_id?}
```

Every route resolves the conversation with `owner_or_404` — a 404 (never 403) for
someone else's thread, matching `get_ticket`'s probe-resistant convention. There is no
admin override.

## Frontend

| File | Role | ~LOC |
|---|---|---|
| `frontend/src/chat/ChatContext.jsx` | Provider: open state, active conversation, messages, streaming. Mounted in `App.jsx` around `<Layout />`, exactly like `NotificationsContext`. | 160 |
| `frontend/src/components/ChatWidget.jsx` | Launcher button + popup panel: header with thread switcher, message list, composer, cost footer. | 250 |
| `frontend/src/components/ChatMessageView.jsx` | One turn: reuses the existing `Markdown.jsx`, plus a collapsed tool-call disclosure and proposed-action cards. | 120 |
| `frontend/src/styles/ChatWidget.module.css` | `position: fixed` bottom-right, using the tokens in `global.css`. | 180 |
| `frontend/src/api.js` | `stream()` + `chatConfig` / `listConversations` / `createConversation` / `getConversation` / `deleteConversation` / `sendChatMessage`. | +40 |
| `frontend/src/components/Layout.jsx` | Mount `<ChatWidget />` after `<main>`. | +2 |

**Ticket awareness without prop drilling:** `Layout` sits outside the `:id` route, so
`ChatWidget` derives the current ticket from `useLocation()` — `/tickets/(\d+)` — and
sends it as `ticket_id` on the first message of a thread. Open the popup on a ticket
page and it already knows what you're looking at; open it on the backlog and it doesn't.

## Threats and limits

- **Prompt injection** — assumed present in every ticket body, comment and log tail.
  Mitigated structurally (no write tools) rather than by prompt wording. Context pack
  sections are fenced and labeled as untrusted data in `prompts.py`.
- **Cross-user leakage** — the only defense that matters is that `user` is never
  model-supplied. Enforced by the `dispatch(name, args, *, db, user)` signature and
  covered by an explicit test.
- **Stored transcripts outlive access** — a thread keeps quoted ticket content after
  the owner is unassigned. Same tradeoff `Notification`'s snapshot fields already make,
  and documented on the model. Deleting the thread deletes the quotes.
- **Cost** — per-IP slowapi limit *and* a per-user daily USD cap summed from
  `ChatMessage.cost_usd` since UTC midnight; over budget returns 429 with the number in
  `detail`, so the popup can say "you've used $0.42 of $0.50 today."
- **Log tails contain source** — same sensitivity as `code_blocks`, behind the same
  `can_view_ticket` gate, and redacted for secrets before upload.

## Tests

Backend (`backend/test_chat.py`, plus additions to `test_agent_runs.py`):

- user B gets 404 on user A's conversation, on GET / POST-message / DELETE
- an admin gets 404 too — no override for chat
- `search_tickets` as a member returns only own/assigned tickets; as admin, all
- `dispatch` cannot be steered to another user (args carrying `user_id` are ignored)
- `ticket_pack` returns `None` for an unviewable ticket, and truncates to budget
- daily USD cap → 429 with the remaining budget in `detail`
- provider unconfigured → `GET /chat/config` reports `enabled: false`; send → 503
- SSE frame sequence, with `provider.stream` monkeypatched to yield canned events
- `propose_action` records to `meta` and performs no write (ticket/comment counts unchanged)
- `log_tail` accepted on `AgentRunCreate`, capped, dropped on a succeeded run, and
  returned only to viewers; the resolver redacts registered secrets before sending

Frontend (`ChatWidget.test.jsx`, vitest + testing-library, matching the existing suites):

- launcher hidden when `chatConfig().enabled` is false
- streamed tokens append to the transcript
- a proposed `create_ticket` card calls `api.createTicket` only on Confirm

## Build order

Four PRs, each independently mergeable and useful:

1. **Provider + config** — ✅ **shipped.** `backend/chat/` (config, budget, context,
   prompts, provider), `backend/routers/chat.py`, `httpx` promoted from a dev to a runtime
   dependency, `GET /chat/config` + non-streaming `POST /chat/ask`. No tables, no UI.
   45 tests in `backend/test_chat.py`; endpoints documented in `api_guide.md`.
2. **Persistence + streaming + popup** — ✅ **shipped.** `ChatConversation` /
   `ChatMessage` + `_migrate_chat_tables`, conversation CRUD, `provider.stream`,
   `chat/spend.py`'s daily cap, SSE from `POST /chat/conversations/{id}/messages`,
   `api.stream` + the chat client in `api.js`, `chat/ChatContext.jsx`, `ChatWidget`,
   `ChatMessageView`, mounted in `Layout`. 77 backend tests; `ChatWidget.test.jsx`
   covers the popup.
3. **Tool loop + proposed actions** — ✅ **shipped.** `backend/ticket_queries.py`
   (the read boundary lifted out of the tickets router so the tools reuse it),
   `chat/tools.py`, `chat/loop.py`, tool-call accumulation in `provider.stream`,
   `CHAT_MAX_TOOL_HOPS`, the `tool_call`/`tool_result` frames and `done.meta`,
   `ProposedAction.jsx` + the disclosure in `ChatMessageView`. **No migration** —
   `ChatMessage.meta` was already the extension point. 350 backend tests.

   Two known limitations, both accepted rather than overlooked:
   *Confirmations are neither idempotent nor recorded* — nothing persists that a
   card was confirmed, so a reload re-renders it and Confirm can be clicked twice
   across sessions. Fixing it needs either a write endpoint (which this design
   forbids, and rightly) or a `meta` mutation endpoint; the actual audit trail is
   the `Activity` row the existing endpoint writes.
   *The per-turn cost ceiling rises ~7×*, bounded by the shared context budget
   and by the loop's running-cost check against the daily cap.
4. **Resolver debugging** — ✅ **shipped.** `AgentRun.log_tail` +
   `_migrate_agent_run_log_tail`, the resolver-side upload through `audit.redact`
   (and the missing secret registrations that fixed), the tail folded into both
   `context._agent_runs` and the `get_agent_runs` tool, and a seeded thread in
   `seed_demo.py` — a failed `implement` run, the assistant explaining the
   `KeyError`, and a proposed follow-up ticket — so the hosted demo shows the
   feature with no `CHAT_API_*` configured. 375 backend tests, 347 resolver.

   Tails are charged against the same budget as everything else and capped per
   run (`LOG_TAIL_CAP`), newest first: a ticket retried three times has three
   failures and the last is the one being asked about. The code fences are
   charged *and* sit outside the clip, so a truncated tail cannot leave the rest
   of the prompt inside an unterminated code block.

## What phase 2 settled that the plan hadn't

- **The stream writes through its own database session.** The request-scoped session from
  `get_db` is closed when the endpoint *returns*, which for a `StreamingResponse` is
  before the body is produced — so the generator opens its own `SessionLocal` and closes
  it in a `finally`.
- **Gates run synchronously, before the response starts.** Once the stream is open the
  status line is already sent, so a late refusal could only be an error frame inside a
  200. Ownership, ticket readability and the budget are therefore all checked in the
  endpoint body, and only provider failures can arrive in-band.
- **Only the live turn carries a context pack.** Replaying old packs would multiply the
  cost and feed the model stale copies of a ticket that has since changed; what history
  contributes is the conversation, not the context.
- **`ChatMessage.content` stores what the user typed, never the assembled prompt.** A
  stored thread therefore can't serve stale ticket state back to the model, and can't
  become a durable copy of a ticket the user has since lost access to.
- **A per-turn `ticket_id` overrides the thread's anchor.** The popup sends whatever
  ticket the user is currently looking at, which is often not the one the thread started
  on. The anchor is re-resolved against the caller's permissions either way.
- **`CHAT_STREAM_USAGE` exists because `stream_options` is not universal.** Most
  OpenAI-compatible gateways accept or ignore the field; a strict one rejects it and
  would break the feature outright, so it has an escape hatch. With it off, streamed
  answers record $0.00 — no usage is reported to price.

## Deferred, deliberately

- **Retrieval/embeddings across the backlog.** `search_tickets` over SQLite is enough at
  this scale; revisit only when a real corpus makes it obviously insufficient.
- **Resolver-side chat** (asking the assistant to drive a worktree). The resolver is a
  cron-swept batch daemon on a dev station and isn't reachable from a browser request.
  The `propose_action` → `/fix` card is the right seam for that instead.
- **Prompt caching** — worth it once the context pack stabilizes, and provider-specific,
  so not in the provider-agnostic first cut.
