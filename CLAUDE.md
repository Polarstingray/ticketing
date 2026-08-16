## Filing a Stingray code-review ticket

> **Resolver runs:** if you are a `resolver/` agent, don't hand-write the `curl`
> below — run `resolver/file_ticket.py` instead. It reads the URL/key from the
> resolver config, validates the fields, and reads `--code-block` content off disk.
> Use `--assign <user_id>` to hand the new ticket to another resolver, and
> `--parent <ticket_id>` when delegating a sub-task (it links the child and makes it
> self-driving). See `resolver/README.md` → "Delegation / fan-out".

> **Terminal sessions: use the `stingray` CLI.** If it's installed (`pipx install
> ./cli`), prefer it over `curl` for everything below — it fills in the `repo:<name>`
> tag from the git checkout you run it in, which hand-written `curl` calls habitually
> omit. Without that tag the resolver has no repo to check out, so it can review only
> the embedded code blocks and cannot apply fixes at all.
>
> ```bash
> stingray review                 # last commit + working tree, hunks attached
> stingray review HEAD~3..HEAD    # an explicit range
> stingray review --describe      # let a local agent write the title/description
> stingray review --assign-bot    # file it straight at the resolver
> stingray file --type task --title "..." --priority low
> ```
>
> `stingray review` turns the diff into `code_blocks` for you, so you never paste code
> by hand. Setting `repo:` needs an API key with the **`cli` scope** (admin-mints it
> from Profile → API keys). See `cli/README.md`.
>
> Falling back: `resolver/file_ticket.py` is the same thing without the git plumbing,
> and the `curl` below is the last resort.

> **Recurring cross-project tasks** (e.g. a security audit you want on every project)
> can be invoked with a **standard command** — put a `/security-audit` line in the
> ticket's description and the resolver injects a premade prompt and runs it. See
> `resolver/README.md` → "Standard commands" and the library in `resolver/commands/`.

When asked to file a review, create a ticket in **Stingray Tickets** via its REST API.

- **Endpoint:** `POST $STINGRAY_URL/api/tickets`
  (`$STINGRAY_URL` is the app's base URL, e.g. `http://localhost:3000`; the `/api`
  prefix is the frontend's proxy to the backend.)
- **Auth:** header `X-API-Key: $STINGRAY_API_KEY`
- **Body (JSON):**
  - `type`: `"code_review"`
  - `title`, `description`
  - `priority`: `low` | `medium` | `high` | `critical`
  - `tags`: string array — **always include `"repo:<repo-name>"`** (the basename of the
    git checkout the code lives in). It tells the resolver which repo to check out; only
    admin/bot API keys may set it, and the reviewed ticket can't be auto-fixed without it.
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
    "tags": ["repo:<repo-name>", "backend"],
    "code_blocks": [
      { "filename": "path/to/file.py", "language": "python",
        "line_start": 10, "line_end": 20, "content": "<the code>" }
    ]
  }'
```

After the resolver reviews a ticket it posts its findings and tags the ticket
`resolver:awaiting-fix`. To have those findings fixed, **don't file a new ticket** —
comment `/fix` (optionally `/fix <notes>`) on the same ticket and re-assign it to the
resolver bot, or press **Apply fixes** on the ticket page. `/review` asks for another
read-only pass.

Full API reference: `api_guide.md`.
