# Stingray Tickets — API Guide

This document describes the complete REST API for **Stingray Tickets**. It is written
primarily so that **Claude Code instances working on other projects** can file *code review*
tickets programmatically, but it documents every endpoint.

All requests and responses are JSON unless noted. The base URL depends on your deployment;
examples below use:

```
BASE=https://tickets.example.com      # behind Traefik in prod
# or for local dev:
BASE=http://localhost:8000
```

---

## Authentication

There are two ways to authenticate. Every endpoint except `GET /health` requires one of them.

### 1. API key (for programmatic / Claude Code use) — **recommended for automation**

Send your key in the `X-API-Key` header:

```bash
curl -s "$BASE/tickets" -H "X-API-Key: sk_your_key_here"
```

Each user can hold **multiple named API keys** (e.g. one per machine or agent). Mint, list,
and revoke them on the **Profile** page in the web UI, or via the
[API-keys endpoints](#api-keys). Keys are stored **hashed** — the plaintext is shown exactly
once, at creation. Keys can be given an optional expiry and revoked at any time; a revoked or
expired key returns `401`. The initial admin's first key (named `default`) is printed to the
backend logs on first startup.

To **rotate** a key with zero downtime: create a new key, swap it into your client, then
revoke the old one.

### 2. Session cookie (for browser use)

`POST /auth/login` sets an httponly signed-cookie session. Subsequent requests reuse the
cookie. With curl, persist it to a cookie jar:

```bash
curl -s -c cookies.txt -X POST "$BASE/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"yourpassword"}'

curl -s -b cookies.txt "$BASE/auth/me"
```

If both an `X-API-Key` header and a session cookie are present, the **API key wins**.

### Errors

| Code | Meaning |
|------|---------|
| 401  | Not authenticated (missing/invalid key or session) |
| 403  | Authenticated but not permitted (e.g. non-admin hitting an admin route) |
| 404  | Resource not found |
| 422  | Validation error (bad/missing field, invalid enum value) |

---

## Quick start: file a code-review ticket from Claude Code

This is the primary automation use case. After completing a task, create a `code_review`
ticket and attach the relevant code as `code_blocks`.

```bash
BASE=https://tickets.example.com
KEY=sk_your_api_key_here

curl -s -X POST "$BASE/tickets" \
  -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "code_review",
    "title": "Review: refactor session handling in auth.py",
    "description": "Switched to stateless signed-cookie sessions. Please review the token helpers.",
    "priority": "high",
    "tags": ["backend", "auth"],
    "code_blocks": [
      {
        "filename": "backend/auth.py",
        "line_start": 60,
        "line_end": 66,
        "language": "python",
        "content": "def create_session_token(user_id: int) -> str:\n    return _serializer.dumps({\"user_id\": user_id})\n\n\ndef read_session_token(token: str):\n    data = _serializer.loads(token, max_age=SESSION_MAX_AGE)\n    return data.get(\"user_id\")"
      }
    ]
  }'
```

**`code_blocks` field shape** (only meaningful for `type: "code_review"`; ignored for tasks):

| Field        | Type   | Notes |
|--------------|--------|-------|
| `filename`   | string | Path to the file, e.g. `backend/auth.py` |
| `line_start` | int ≥1 | First line of the range (1-based) |
| `line_end`   | int ≥1 | Last line of the range (inclusive) |
| `content`    | string | The code text. Use `\n` for newlines in JSON. The viewer numbers lines starting at `line_start`. |
| `language`   | string | Highlight.js language id, e.g. `python`, `javascript`, `bash`. Defaults to `plaintext`. |

The response is the created ticket (see [Ticket object](#ticket-object)).

---

## Endpoints

### Auth

#### `POST /auth/login`
Set a session cookie.

Request:
```json
{ "username": "admin", "password": "yourpassword" }
```
Response `200`: the [user object (self)](#user-object). Sets a `session` cookie.
`401` if credentials are wrong.

#### `POST /auth/logout`
```bash
curl -s -b cookies.txt -X POST "$BASE/auth/logout"
```
Response `200`: `{ "ok": true }`. Clears the cookie.

#### `GET /auth/me`
```bash
curl -s -H "X-API-Key: $KEY" "$BASE/auth/me"
```
Response `200`: the [user object (self)](#user-object). API keys are not embedded — manage
them via the [API-keys endpoints](#api-keys).

---

### Tickets

#### `GET /tickets`
List tickets, **paginated**. All filters are optional query params and combine (AND).

| Param         | Values |
|---------------|--------|
| `status`      | `open` `in_review` `changes_requested` `resolved` `closed` |
| `type`        | `code_review` `task` |
| `assigned_to` | user id (int) |
| `created_by`  | user id (int) |
| `priority`    | `low` `medium` `high` `critical` |
| `tag`         | a tag string, matched exactly. **Repeatable** — pass `tag` more than once to filter on several tags at a time. |
| `tag_match`   | `all` (default) `any`; how several `tag` params combine. `all` requires every tag, `any` matches tickets carrying at least one. Ignored with fewer than two tags. |
| `q`           | free-text search; case-insensitive substring match over `title` or `description` (blank/whitespace-only ignored) |
| `archived`    | `true` `false`; omitted by default, which **hides** archived tickets. Pass `true` for the archive view, `false` to list only non-archived. |
| `sort`        | `created` (default) `updated` `priority` `due` `title` |
| `order`       | `desc` (default) `asc`. For `priority`, `desc` means most urgent first; tickets with no `due_date` always sort last. |
| `limit`       | page size, 1–200 (default `50`) |
| `offset`      | number to skip (default `0`) |

```bash
curl -s -H "X-API-Key: $KEY" "$BASE/tickets?type=code_review&status=open&limit=20&offset=0"

# Tickets that are tagged BOTH `backend` and `urgent`, most urgent first:
curl -s -H "X-API-Key: $KEY" "$BASE/tickets?tag=backend&tag=urgent&sort=priority"

# Tickets tagged EITHER `backend` or `frontend`:
curl -s -H "X-API-Key: $KEY" "$BASE/tickets?tag=backend&tag=frontend&tag_match=any"
```
Response `200`: a **paginated envelope** — `items` is the page, `total` is the full count
across all pages (ignoring `limit`/`offset`):
```json
{ "items": [ /* ticket objects */ ], "total": 137, "limit": 20, "offset": 0 }
```
To page, increase `offset` by `limit` until `offset + items.length >= total`.

#### `GET /tickets/tags`
Every tag in use across the tickets **you can see**, with a usage count — the tag picker's
data source. Honors the same visibility rules as `GET /tickets`, so it never reveals a tag
that exists only on someone else's ticket.

| Param      | Values |
|------------|--------|
| `archived` | `true` `false`; as on `GET /tickets`, archived tickets are excluded by default |

```bash
curl -s -H "X-API-Key: $KEY" "$BASE/tickets/tags"
```
Response `200`, ordered by count (descending), ties broken alphabetically:
```json
{ "items": [ { "tag": "repo:ticketing", "count": 42 }, { "tag": "bug", "count": 9 } ] }
```

#### `POST /tickets`
Create a ticket. `created_by` is set to the authenticated user automatically.

Request body:

| Field         | Type     | Required | Default    |
|---------------|----------|----------|------------|
| `type`        | enum     | yes      | —          |
| `title`       | string   | yes      | —          |
| `description` | string   | no       | `""`       |
| `priority`    | enum     | no       | `medium`   |
| `status`      | enum     | no       | `open`     |
| `assigned_to` | int/null | no       | `null`     |
| `due_date`    | ISO 8601 datetime / null | no | `null` |
| `code_blocks` | array    | no       | `[]` (kept only for `code_review`) |
| `tags`        | string[] | no       | `[]`       |

```bash
curl -s -X POST "$BASE/tickets" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"type":"task","title":"Take out the trash","priority":"low","tags":["chores"],"due_date":"2026-06-10T18:00:00Z"}'
```
Response `201`: the created [ticket object](#ticket-object).

#### `GET /tickets/{id}`
```bash
curl -s -H "X-API-Key: $KEY" "$BASE/tickets/1"
```
Response `200`: [ticket object](#ticket-object). `404` if not found.

#### `PATCH /tickets/{id}`
Partial update — send only the fields you want to change. Accepts any of: `title`,
`description`, `status`, `priority`, `assigned_to`, `due_date`, `tags`, `code_blocks`.
Bumps `updated_at`.

**Permission:** admins may edit any ticket; members may edit only tickets they created or
are assigned to (otherwise `403`).

```bash
curl -s -X PATCH "$BASE/tickets/1" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"status":"in_review","assigned_to":2}'
```
Response `200`: the updated [ticket object](#ticket-object).

#### `POST /tickets/{id}/archive`
Archive a ticket so it disappears from the default list without deleting it. The ticket
**must be `closed`** (otherwise `400`). Bumps `updated_at`.

**Permission:** admins may archive any ticket; members may archive only tickets they created
or are assigned to (otherwise `403`).

```bash
curl -s -X POST "$BASE/tickets/1/archive" -H "X-API-Key: $KEY"
```
Response `200`: the updated [ticket object](#ticket-object) with `"archived": true`.
`400` if the ticket is not closed, `404` if not found.

#### `POST /tickets/{id}/unarchive`
Restore an archived ticket to the default list (sets `archived` back to `false`). No status
restriction. Same permission model as archive.

```bash
curl -s -X POST "$BASE/tickets/1/unarchive" -H "X-API-Key: $KEY"
```
Response `200`: the updated [ticket object](#ticket-object) with `"archived": false`.

#### `DELETE /tickets/{id}`
**Admin only.** Permanently deletes the ticket. To merely hide a closed ticket, prefer
`POST /tickets/{id}/archive`.
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE "$BASE/tickets/1" -H "X-API-Key: $ADMIN_KEY"
```
Response `204` on success, `403` for non-admins.

---

### Comments

#### `GET /tickets/{id}/comments`
Returns the comment thread, oldest first.
```bash
curl -s -H "X-API-Key: $KEY" "$BASE/tickets/1/comments"
```
Response `200`: array of [comment objects](#comment-object).

#### `POST /tickets/{id}/comments`
```bash
curl -s -X POST "$BASE/tickets/1/comments" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"body":"Looks good — addressed the null check, please re-review."}'
```
Response `201`: the created [comment object](#comment-object).

---

### Activity

#### `GET /tickets/{id}/activity`
Returns the ticket's audit trail (creation, assignment, status/priority changes, comments),
oldest first.
```bash
curl -s -H "X-API-Key: $KEY" "$BASE/tickets/1/activity"
```
Response `200`: array of [activity objects](#activity-object).

---

### Leases (claiming a ticket)

A **lease** is an exclusive, expiring claim on one ticket. It exists so that two workers —
two resolver sweeps, or a resolver and a third-party agent — can't pick up the same ticket
and do the work twice. The lease row is the source of truth; the `resolver:claimed` tag
mirrored onto the ticket is advisory, for humans reading it.

The claim carries a **TTL**. A worker that dies mid-ticket therefore has its claim lapse and
the ticket returns to the queue, rather than being stranded forever under an in-flight
`resolver:*` tag. A worker with a long job keeps its claim by heartbeating (`extend`) before
the TTL runs out; if it stops heartbeating it loses the ticket. An expired lease **cannot be
resurrected** — go round again via `claim`.

`ttl_seconds` is 5–3600 (default 300). **Permission:** the same authority as modifying the
ticket (admin, creator, or assignee); anyone else gets `404`.

#### `POST /tickets/{id}/claim`
```bash
curl -s -X POST "$BASE/tickets/1/claim" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"ttl_seconds": 600}'
```
Response `200`:
```json
{ "ticket_id": 1, "worker_id": 2, "token": "gT4…", "expires_at": "2026-01-01T12:10:00+00:00" }
```
`409` if another worker already holds a live lease (the detail names the holder) — the
correct response is to move on to the next ticket. `404` if the ticket doesn't exist or the
caller may not work it.

Keep the `token`: it is returned only to the claimant and is required to release or extend.

#### `POST /tickets/{id}/lease/extend`
The heartbeat. Pushes `expires_at` out from *now*.
```bash
curl -s -X POST "$BASE/tickets/1/lease/extend" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"token":"gT4…","ttl_seconds":600}'
```
Response `200`: the lease object, as above. `404` if the lease has already expired or was
released; `403` if the token belongs to a different holder.

#### `POST /tickets/{id}/release`
Give the claim back early, so the ticket is workable again without waiting out the TTL.
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/tickets/1/release" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"token":"gT4…"}'
```
Response `204`. `404` if there is no live lease (it had already expired — not an error worth
handling), `403` on a token mismatch.

#### Writing results under a lease
`POST /tickets/{id}/agent-runs` accepts an optional `lease_token`. Send it and the write is
refused with `409` once the lease has lapsed, which is what stops a worker that stalled past
its TTL from posting results over whoever re-claimed its ticket. Omit it and the endpoint
behaves exactly as before (the assignee check alone).

---

### Events

#### `GET /events/stream`
A [Server-Sent Events](https://developer.mozilla.org/docs/Web/API/Server-sent_events)
tail of the ticket event log, so a client can react to a change in about a second
instead of polling for it. The connection is long-lived and the client dials out, which
means an automation behind NAT needs no open port.

**Visibility is the same rule as `GET /tickets`**: a non-admin sees only events on
tickets they created or are assigned to, and admins see everything. The rule is applied
against the ticket as it is *now*, not as it was when the event was recorded, so a ticket
reassigned away stops appearing.

| Param | Meaning |
|---|---|
| `last_event_id` | Resume *after* this event id. Omit to start at the current head. |

A fresh connection starts at the head and does **not** replay history — replaying to
every client that connects turns a reconnect storm into a stampede. To cover a gap, pass
back the `id:` of the last event you processed, either as `last_event_id` or as the
standard `Last-Event-ID` header (which `EventSource` resends automatically).

```bash
curl -sN -H "X-API-Key: $KEY" "$BASE/events/stream?last_event_id=41"
```

```
: connected at 41

id: 42
event: ticket.assigned
data: {"ticket_id": 7, "ticket_title": "Fix the thing", "ticket_status": "open",
       "ticket_priority": "high", "ticket_type": "task", "ticket_tags": ["repo:ticketing"],
       "assigned_to": 2, "actor_id": 1, "actor_name": "Alice",
       "delta": {"to": 2, "name": "claude-bot"}, "event_id": 42, "type": "ticket.assigned"}
```

(`data` is one line on the wire; it is wrapped here for readability. `delta` carries the
before/after of a change event and is absent on creation events.)

Event types: `ticket.created`, `ticket.assigned`, `ticket.status_changed`,
`ticket.tagged`, `comment.created`, `agent_run.finished`. A `:` line is a comment — the
15-second keepalive that holds the connection open through proxies — and carries no data.

**`data` is a hint, not truth.** It is a snapshot from the moment the change committed,
and a consumer may see it well after the fact, so re-fetch the ticket before acting on
it. Treat delivery as at-least-once: handle a repeat of an event you have already seen.

`resolver/listen.py` is a worked example — it follows this stream and wakes the ticket
resolver on assignment.

### Chat assistant

An AI assistant that answers questions about a ticket. **Optional**: it is off unless the
deployment sets `CHAT_API_URL`, `CHAT_API_KEY` and `CHAT_API_MODEL` (see `.env.example`),
and `GET /chat/config` is how a client discovers that.

It **never writes**. It has read-only tools — searching tickets, reading one, reading a
ticket's resolver runs — and it can *propose* an action (file a ticket, post a comment,
change a status), which renders a card the user confirms; confirming calls the ordinary
endpoints below as the signed-in user. There are no assistant-only write routes.

Every tool resolves against **the caller's own** read permissions, so a ticket you may not
view returns `404` — the same answer `GET /tickets/{id}` gives, so neither the endpoint nor
a tool can be used to probe ticket ids. The model chooses *what* to ask about; it never
chooses *who is asking*.

#### `GET /chat/config`
Whether the assistant is available, and which model answers. The endpoint URL and API key
are never exposed.
```bash
curl -s -H "X-API-Key: $KEY" "$BASE/chat/config"
```
Response `200`: `{ "enabled": true, "model": "claude-sonnet-5" }`

#### `POST /chat/ask`
Ask one question, optionally anchored to a ticket. There is no conversation state — each
call stands alone.

| Field | Type | Notes |
|---|---|---|
| `question` | string | required, 1–4000 chars |
| `ticket_id` | int | optional; attaches that ticket's context |

```bash
curl -s -X POST "$BASE/chat/ask" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"question": "Why did the resolver stop on this?", "ticket_id": 42}'
```
Response `200`:
```json
{
  "answer": "The implement run failed before it opened a PR ...",
  "usage": {"model": "claude-sonnet-5", "input_tokens": 4210,
            "output_tokens": 180, "cost_usd": 0.015330},
  "context_ticket_id": 42,
  "context_chars": 18442
}
```
`usage.cost_usd` is priced from the deployment's configured per-1M-token rates, and reads
`0.0` when those are unset. `context_chars` is how much ticket context was actually sent —
a large ticket is truncated to the configured budget rather than rejected.

Errors: `404` ticket not found or not yours · `422` blank/oversized question ·
`429` rate limited (per-IP, or the provider's own quota) · `503` assistant not configured ·
`502`/`504` the model provider failed or timed out.

#### Conversations

Threads persist per user and are **strictly private — admins included**. Every route
answers `404` for a thread you don't own.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/chat/conversations` | your threads, most recently active first |
| `POST` | `/chat/conversations` | `{ "ticket_id": 42 }` (optional) → `201` |
| `GET` | `/chat/conversations/{id}` | the thread with its full transcript |
| `DELETE` | `/chat/conversations/{id}` | `204`; cascades to the messages |

A thread's `ticket_id` is an *anchor*, not a grant: it is re-resolved against the
caller's own read permissions on every turn, so losing access to the ticket stops the
thread with a `404`.

#### `POST /chat/conversations/{id}/messages`

Ask a question in a thread. The answer streams back as **Server-Sent Events**.

| Field | Type | Notes |
|---|---|---|
| `content` | string | required, 1–4000 chars |
| `ticket_id` | int | optional; overrides the thread's anchor for this turn |

Every gate — ownership, the ticket's readability, the daily budget — is checked before
the stream opens, so refusals are real HTTP statuses rather than error frames.

```bash
curl -N -X POST "$BASE/chat/conversations/3/messages" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"content": "Why did the implement run fail?"}'
```

```
event: token
data: {"text": "The implement run "}

event: token
data: {"text": "failed before opening a PR."}

event: done
data: {"message_id": 91, "conversation_id": 3, "title": "Why did the implement run fail?",
       "usage": {"model": "claude-sonnet-5", "input_tokens": 4210,
                 "output_tokens": 180, "cost_usd": 0.015330},
       "spent_today_usd": 0.0421}
```

An `error` frame (`{"detail": "...", "status": 502}`) means the failure happened after
the stream opened. The question is still recorded; no answer is invented.

Note `EventSource` cannot be used here — it is GET-only and cannot carry a body. Read the
stream with `fetch` and a reader (see `frontend/src/api.js`).

Errors: `404` thread or ticket not found or not yours · `422` blank/oversized content ·
`429` rate limited **or the daily USD cap reached** (the detail names the numbers) ·
`503` assistant not configured.

---

### Saved views

Named, reusable dashboard filters. A view stores the ticket list's **raw query string**
(the same one `GET /tickets` takes), so saving a view and sharing a filtered link are the
same thing. The stored `query` is opaque to the server — it is echoed back and applied by
the client, never parsed or executed.

Every route is scoped to the authenticated user: you can only see and modify your own
views, and someone else's id returns `404` (not `403`, which would confirm it exists).
Admins are not exempt — these are personal, not moderated content.

#### `GET /saved-views`
Your saved views, ordered by name.
```bash
curl -s -H "X-API-Key: $KEY" "$BASE/saved-views"
```
Response `200`:
```json
[ { "id": 1, "name": "My open bugs", "query": "status=open&tag=bug&sort=priority",
    "created_at": "...", "updated_at": "..." } ]
```

#### `POST /saved-views`
| Field   | Type   | Required | Notes |
|---------|--------|----------|-------|
| `name`  | string | yes      | 1–60 chars, unique per user |
| `query` | string | no       | max 1000 chars; a leading `?` is stripped. Defaults to `""`. |

```bash
curl -s -X POST "$BASE/saved-views" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name": "My open bugs", "query": "status=open&tag=bug&sort=priority"}'
```
Response `201`: the saved view. `409` if you already have a view by that name; `422` past
50 views.

#### `PATCH /saved-views/{id}`
Partial update — send `name`, `query`, or both. `409` on a name you already use.

#### `DELETE /saved-views/{id}`
Response `204`.

---

### Webhooks

Register an HTTPS endpoint that receives ticket events, and read the log of what was
delivered to it. **Delivery execution ships separately** — these endpoints store
subscriptions and the delivery log; nothing is sent yet.

Every route is scoped to the authenticated user (an admin may reach any webhook);
someone else's id returns `404`, not `403`.

**The signing secret is shown exactly once.** It is returned by `POST /webhooks` and by
`POST /webhooks/{id}/rotate-secret`, and by nothing else — every read path carries only
`secret_prefix` (its first 8 characters) as a label. Unlike an API key it is stored in
plaintext, because the delivery worker must be able to *sign* with it; the protection is
that it is never readable back. Lose it and you rotate.

#### URL rules (SSRF)

A webhook is an outbound request the server makes to an address you chose, so the URL is
checked at creation, at update, and again immediately before each delivery (DNS can
change in between). A rejection is a `422` whose `detail` begins `webhook URL rejected:`
and names the reason. Rejected:

- any scheme but `https` (plain `http` only when the server sets `ALLOW_INSECURE_WEBHOOKS=1`);
- userinfo (`https://user:pass@…`), a fragment, a URL over 2000 chars;
- any port other than 80, 443, 8080, 8443;
- a host that **is**, or **resolves to**, a loopback, private, link-local (including the
  cloud metadata addresses `169.254.169.254` / `fd00:ec2::254`), unique-local, multicast,
  reserved or unspecified address — *every* resolved address must pass, not just the
  first, which is what closes DNS rebinding;
- hosts named `localhost`, `metadata.google.internal`, or ending `.internal` / `.local`;
- a host that does not resolve at all.

#### `GET /webhooks`
Your webhooks, newest first. Admins may pass `?user_id=` to scope to another user.

#### `POST /webhooks`
| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | 1–60 chars |
| `url` | string | yes | see [URL rules](#url-rules-ssrf) |
| `event_types` | string[] | no | subset of the [webhook event types](#enumerations); `[]` (default) = all |
| `tag_filter` | string[] | no | max 20; the ticket must carry **any** of them. `[]` = every ticket |
| `active` | bool | no | defaults `true` |

```bash
curl -s -X POST "$BASE/webhooks" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"ci","url":"https://example.com/hooks/stingray",
       "event_types":["ticket.created"],"tag_filter":["repo:my-app"]}'
```
Response `201`: the webhook **plus** a one-time `secret`. `422` past 20 webhooks, or on a
rejected URL.

#### `GET /webhooks/{id}` · `PATCH /webhooks/{id}` · `DELETE /webhooks/{id}`
`PATCH` is partial (`name`, `url`, `event_types`, `tag_filter`, `active`); a new `url` is
re-validated. Re-activating a paused webhook resets `consecutive_failures` to 0. `DELETE`
returns `204` and removes its delivery log with it.

#### `POST /webhooks/{id}/rotate-secret`
Response `200`: `{ "id": 1, "secret": "…", "secret_prefix": "…" }` — the new secret,
shown once. The old one stops signing immediately.

#### `GET /webhooks/{id}/deliveries`
The delivery log, newest first. Params: `state` (a [delivery state](#enumerations)),
`ticket_id`, `limit` (default 50, max 200), `offset`.

**Visibility:** rows are filtered against what the webhook's **owner** may see — the same
boundary as `GET /tickets` — so a member's webhook can never surface a ticket they could
not open. This is keyed on the owner deliberately: an admin reading a member's log sees
only what the member could.

Response `200`: `{ items, total, limit, offset }`, each item:
```json
{ "id": 11, "webhook_id": 1, "event_id": 340, "event_type": "ticket.created",
  "ticket_id": 42, "attempt_count": 2, "next_attempt_at": null, "status_code": 500,
  "response_snippet": "upstream error", "error": "", "state": "failed",
  "created_at": "...", "updated_at": "..." }
```

#### `POST /webhooks/{id}/deliveries/{delivery_id}/redeliver`
Re-arms a delivery for another attempt: `state` back to `pending`, `next_attempt_at` to
now, `status_code`/`error` cleared. `attempt_count` is **kept** — it is history. The URL
is re-validated first (`422` if it no longer passes). Returns the delivery. Nothing is
sent by this call; the delivery worker does that.

---

### Users

#### `GET /users` — **admin only**
List all users (public shape, no API keys).
```bash
curl -s -H "X-API-Key: $ADMIN_KEY" "$BASE/users"
```

#### `POST /users` — **admin only**
Create a user. New users start with **no** API keys — they (or an admin) mint one via the
[API-keys endpoints](#api-keys).

Request:
```json
{ "username": "alice", "display_name": "Alice", "email": "alice@example.com",
  "password": "atleast6chars", "role": "member" }
```
Response `201`: [user object (self)](#user-object). `400` if the username already exists.

#### `PATCH /users/{id}`
Edit a user. A user may edit **themselves**; admins may edit anyone. Fields: `display_name`,
`email`, `password`. The `role` field is honored **only for admin callers** (`403` otherwise).
```bash
curl -s -X PATCH "$BASE/users/2" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"display_name":"Alice B."}'
```
Response `200`: [user object (self)](#user-object).

#### `DELETE /users/{id}` — **admin only**
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE "$BASE/users/2" -H "X-API-Key: $ADMIN_KEY"
```
Response `204`. You cannot delete your own account (`400`).

---

### API keys

All three routes are **self or admin** (you may manage your own keys; admins may manage
anyone's). `{user_id}` is the owning user's id.

#### `GET /users/{user_id}/api-keys`
List a user's keys (metadata only — never the secret), newest first.
```bash
curl -s -H "X-API-Key: $KEY" "$BASE/users/2/api-keys"
```
Response `200`: array of [API-key objects](#api-key-object).

#### `POST /users/{user_id}/api-keys`
Mint a new key. The plaintext `api_key` is returned **once** in this response and never again.

Request:
```json
{ "name": "claude-code-laptop", "expires_in_days": 90, "scopes": ["cli"] }
```
`name` is required; `expires_in_days` is optional (omit for a non-expiring key).
```bash
curl -s -X POST "$BASE/users/2/api-keys" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"claude-code-laptop"}'
```
Response `201`: an [API-key object](#api-key-object) **plus** an `api_key` field with the
plaintext key.

##### Scopes

`scopes` is optional and **admin-only** — a non-admin passing it gets `403`, even for
their own key. That restriction is the point: any member may mint their own keys, so a
self-granted scope would be no boundary at all.

| scope | grants |
|---|---|
| `cli` | may set the *aiming* tags `repo:<name>`, `rev:<sha>`, `branch:<name>` — and no other reserved tag |
| `agent` | may set the *routing* tags `parent:<id>`, `review-by:<id>`, and register via `POST /agents/heartbeat` — and nothing else |

A scope is carried by the **key**, not the user, so revoking the key revokes the
capability, and the same user's browser session does not inherit it. `cli` exists so the
`stingray` CLI can tag the repo a review belongs to (which is what lets a resolver bot
check the code out) without its owner being an admin.

`agent` is for a **third-party worker**, and is narrower than `cli` rather than a
superset: an external agent needs to record how its work relates to other work, never to
point this app's automation at a checkout of its choosing. See
[docs/external-agents.md](docs/external-agents.md) for the full register → subscribe →
claim → report flow.

Unknown scope names are rejected with `422`.

#### `POST /users/{user_id}/api-keys/{key_id}/revoke`
Permanently revoke a key. Anything using it immediately gets `401`.
```bash
curl -s -X POST "$BASE/users/2/api-keys/5/revoke" -H "X-API-Key: $KEY"
```
Response `200`: the updated [API-key object](#api-key-object) (`revoked: true`).

---

## Giving a Claude Code instance access

The recommended setup uses two environment variables, so nothing secret is hard-coded:

```bash
export STINGRAY_URL=https://tickets.example.com
export STINGRAY_API_KEY=sk_...        # a dedicated key, e.g. minted as "claude-code-laptop"
```

Mint **one named key per machine/agent** on your Profile page (so you can revoke a single
machine without disrupting others), and keep the key out of version control (shell profile or
a git-ignored `.env`). Drop this snippet into a repo's `CLAUDE.md` so any Claude Code instance
working there knows how to file a review:

```md
## Filing a code-review ticket
When asked to file a review, POST to $STINGRAY_URL/api/tickets with header
`X-API-Key: $STINGRAY_API_KEY`. Body: {type:"code_review", title, description,
priority, tags:[], assigned_to:<reviewer id>, code_blocks:[{filename, language,
line_start, line_end, content}]}. Capture the exact files/line ranges you changed.
```

**The task → review loop:**
1. Ask the AI to do the work (ideally on a branch).
2. On completion it self-files a `code_review` ticket via the API, putting the changed files
   and line ranges into `code_blocks`, tagging it, and setting `assigned_to` a human reviewer.
3. The reviewer is emailed (see [Notifications](#notifications)), reviews in the UI with the
   flagged line ranges highlighted, comments, and sets `changes_requested` or `resolved`.
4. Every step is captured in the ticket's [activity trail](#activity).

---

## Notifications

If the server is configured with SMTP (`SMTP_HOST` etc.), Stingray sends best-effort emails:

- **Ticket assigned** → the new assignee (never the person who assigned it).
- **New ticket created** → all admins (except the creator).

Email is entirely optional; with SMTP unconfigured the API behaves identically, just without
sending mail.

---

## Object shapes

### Ticket object
```json
{
  "id": 1,
  "type": "code_review",
  "title": "Review: refactor session handling in auth.py",
  "description": "Switched to stateless signed-cookie sessions.",
  "status": "open",
  "priority": "high",
  "archived": false,
  "created_by": 1,
  "assigned_to": null,
  "created_at": "2026-06-04T19:08:59.376527",
  "updated_at": "2026-06-04T19:08:59.376536",
  "due_date": null,
  "code_blocks": [
    { "filename": "backend/auth.py", "line_start": 60, "line_end": 66,
      "content": "def create_session_token(...):\n    ...", "language": "python" }
  ],
  "tags": ["backend", "auth"]
}
```

### Comment object
```json
{ "id": 1, "ticket_id": 1, "author": 1,
  "body": "Looks good, ship it", "created_at": "2026-06-04T19:08:59.666872" }
```

### Activity object
`actor` is the user id who performed the action (or `null`). `detail` varies by `action`:
`status_changed`/`priority_changed` carry `{from, to}`; `assigned` carries `{to, name}`;
`commented` carries `{comment_id}`; `created`/`unassigned` have `null`.
```json
{ "id": 7, "ticket_id": 1, "actor": 1, "action": "status_changed",
  "detail": { "from": "open", "to": "in_review" },
  "created_at": "2026-06-04T19:09:10.112233" }
```

### API-key object
Metadata only. `POST /users/{id}/api-keys` additionally returns a one-time `api_key`
(plaintext) alongside these fields; no other endpoint ever returns the secret.
```json
{
  "id": 5, "name": "claude-code-laptop", "key_prefix": "sk_URA6YaKc",
  "created_at": "2026-06-04T19:10:38.015980",
  "last_used_at": "2026-06-04T19:10:38.296683",
  "expires_at": null, "revoked": false, "scopes": ["cli"]
}
```
`scopes` is `[]` for an ordinary key. See [Scopes](#scopes) for what they grant.

### User object
The **self** shape (returned from `/auth/me`, `/auth/login`, user create/update). The
**public** shape (from `GET /users`) is identical. API keys are never embedded in either.
```json
{
  "id": 1, "username": "admin", "display_name": "admin",
  "email": "admin@example.com", "role": "admin",
  "created_at": "2026-06-04T19:08:38.425332"
}
```

## Enumerations

- **type:** `code_review`, `task`
- **status:** `open`, `in_review`, `changes_requested`, `resolved`, `closed`
- **priority:** `low`, `medium`, `high`, `critical`
- **role:** `admin`, `member`
- **webhook event type:** `ticket.created`, `ticket.assigned`, `ticket.status_changed`,
  `ticket.tagged`, `comment.created`, `agent_run.finished`
- **delivery state:** `pending`, `delivering`, `succeeded`, `failed` (retries exhausted),
  `skipped` (never sent — e.g. the URL failed re-validation)
