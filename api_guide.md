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

Each user has exactly one API key. View or regenerate it from the **Profile** page in the
web UI, or via `POST /users/{id}/regenerate-api-key`. The initial admin's key is printed to
the backend logs on first startup.

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
Response `200`: the [user object (self)](#user-object), including your `api_key`.

---

### Tickets

#### `GET /tickets`
List tickets, newest first. All filters are optional query params and combine (AND).

| Param         | Values |
|---------------|--------|
| `status`      | `open` `in_review` `changes_requested` `resolved` `closed` |
| `type`        | `code_review` `task` |
| `assigned_to` | user id (int) |
| `created_by`  | user id (int) |
| `priority`    | `low` `medium` `high` `critical` |
| `tag`         | a single tag string; matches tickets containing that tag |

```bash
curl -s -H "X-API-Key: $KEY" "$BASE/tickets?type=code_review&status=open&priority=high"
```
Response `200`: array of [ticket objects](#ticket-object).

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

### Users

#### `GET /users` — **admin only**
List all users (public shape, no API keys).
```bash
curl -s -H "X-API-Key: $ADMIN_KEY" "$BASE/users"
```

#### `POST /users` — **admin only**
Create a user. An API key is auto-generated.

Request:
```json
{ "username": "alice", "display_name": "Alice", "email": "alice@example.com",
  "password": "atleast6chars", "role": "member" }
```
Response `201`: [user object (self)](#user-object) including the new `api_key`.
`400` if the username already exists.

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

#### `POST /users/{id}/regenerate-api-key`
Generate a new API key (invalidating the old one). Self or admin.
```bash
curl -s -X POST "$BASE/users/2/regenerate-api-key" -H "X-API-Key: $KEY"
```
Response `200`: `{ "api_key": "sk_..." }`.

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

### User object
The **self** shape (returned from `/auth/me`, `/auth/login`, user create/update, and to
admins) includes `api_key`. The **public** shape (from `GET /users`) omits it.
```json
{
  "id": 1, "username": "admin", "display_name": "admin",
  "email": "admin@example.com", "role": "admin",
  "created_at": "2026-06-04T19:08:38.425332",
  "api_key": "sk_..."
}
```

## Enumerations

- **type:** `code_review`, `task`
- **status:** `open`, `in_review`, `changes_requested`, `resolved`, `closed`
- **priority:** `low`, `medium`, `high`, `critical`
- **role:** `admin`, `member`
