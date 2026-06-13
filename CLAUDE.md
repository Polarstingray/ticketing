## Filing a Stingray code-review ticket

> **Resolver runs:** if you are a `resolver/` agent, don't hand-write the `curl`
> below — run `resolver/file_ticket.py` instead. It reads the URL/key from the
> resolver config, validates the fields, and reads `--code-block` content off disk.
> Use `--assign <user_id>` to hand the new ticket to another resolver, and
> `--parent <ticket_id>` when delegating a sub-task (it links the child and makes it
> self-driving). See `resolver/README.md` → "Delegation / fan-out".

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
