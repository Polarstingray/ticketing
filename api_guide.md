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
List tickets, newest first, **paginated**. All filters are optional query params and combine
(AND).

| Param         | Values |
|---------------|--------|
| `status`      | `open` `in_review` `changes_requested` `resolved` `closed` |
| `type`        | `code_review` `task` |
| `assigned_to` | user id (int) |
| `created_by`  | user id (int) |
| `priority`    | `low` `medium` `high` `critical` |
| `tag`         | a single tag string; matches tickets containing that exact tag |
| `limit`       | page size, 1–200 (default `50`) |
| `offset`      | number to skip (default `0`) |

```bash
curl -s -H "X-API-Key: $KEY" "$BASE/tickets?type=code_review&status=open&limit=20&offset=0"
```
Response `200`: a **paginated envelope** — `items` is the page, `total` is the full count
across all pages (ignoring `limit`/`offset`):
```json
{ "items": [ /* ticket objects */ ], "total": 137, "limit": 20, "offset": 0 }
```
To page, increase `offset` by `limit` until `offset + items.length >= total`.

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

#### `DELETE /tickets/{id}`
**Admin only.**
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
{ "name": "claude-code-laptop", "expires_in_days": 90 }
```
`name` is required; `expires_in_days` is optional (omit for a non-expiring key).
```bash
curl -s -X POST "$BASE/users/2/api-keys" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"claude-code-laptop"}'
```
Response `201`: an [API-key object](#api-key-object) **plus** an `api_key` field with the
plaintext key.

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
  "expires_at": null, "revoked": false
}
```

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
