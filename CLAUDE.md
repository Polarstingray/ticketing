## Filing a follow-up review ticket from the resolver

> **If the resolver is doing the work, prefer the `/ticket` directive.** When the
> Stingray resolver implements a ticket, it can file the follow-up `code_review`
> ticket itself — deterministically, through its hardened `StingrayClient`, with
> `code_blocks` pulled from the real git diff. A human just leaves a `/ticket
> [options]` comment (peer to `/approve`) on the source ticket; see
> `resolver/README.md` → "Filing a follow-up review ticket (`/ticket`)". This
> replaces asking a headless Claude to `curl` the API by hand (which was
> unreliable). The manual `curl` recipe below is the fallback for ad-hoc use
> outside the resolver.

## Filing a Stingray code-review ticket (manual)

When asked to file a review, create a ticket in **Stingray Tickets** via its REST API.

- **Endpoint:** `POST $STINGRAY_URL/api/tickets`
  (`$STINGRAY_URL` is the app's base URL, e.g. `http://localhost:3000`; the `/api`
  prefix is the frontend's proxy to the backend.)
- **Auth:** header `X-API-Key: $STINGRAY_API_KEY`
- **Body (JSON):**
  - `type`: `"code_review"`
  - `title`, `description`
  - `priority`: `low` | `medium` | `high` | `critical`
  - `tags`: string array
  - `assigned_to`: reviewer's user id (optional)
  - `code_blocks`: array of `{ filename, language, line_start, line_end, content }` —
    capture the **exact files and line ranges you changed**.

Example:

```bash
curl -s -X POST "$STINGRAY_URL/api/tickets" \
  -H "X-API-Key: $STINGRAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "code_review",
    "title": "Review: <what changed>",
    "description": "<why, and what to look at>",
    "priority": "medium",
    "tags": ["backend"],
    "code_blocks": [
      { "filename": "path/to/file.py", "language": "python",
        "line_start": 10, "line_end": 20, "content": "<the code>" }
    ]
  }'
```

Full API reference: `api_guide.md`.
