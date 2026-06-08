# Stingray ticket resolver

A standalone companion tool that lets you **hand a ticket to a Claude Code
instance**. You file a ticket assigned to a dedicated `claude-bot` user; the
resolver (run on a cron sweep) picks it up, **proposes a plan**, waits for your
`/approve`, then **implements the fix on a branch and opens a PR** — leaving the
plan, the summary, and every status change in the ticket's trail.

It talks only to the existing Stingray REST API (see `../api_guide.md`) — no
backend changes. All code execution happens in an isolated `git worktree`, so
your live checkouts under `PROJECTS_ROOT` are never touched.

## Flow

```
file ticket (assigned to claude-bot, status=open)
   └─ resolver: Claude PLANS (read-only) ─→ posts plan comment,
                                            status=in_review, reassigned to YOU  ──┐
                                                                                   │ email
   you read the plan, reassign to claude-bot, comment:                            │
     /approve            → resolver: Claude IMPLEMENTS → opens PR ───┐            │
     /revise <notes>     → resolver: re-plans (loops above)          │            │
                                                                     ▼            │
   resolver: posts PR link, status=in_review, reassigned to YOU ◄───┘ ───────────┘
   you review/merge the PR → set resolved   (or changes_requested → reworks the PR)
```

Notifications: Stingray only emails on **assignment**, so each hand-off is an
assignment flip — you get "assigned to you" mail when a plan or PR is ready.

**Dangerous mode:** tag a ticket `dangerous` to skip the plan gate — the resolver
implements and opens a PR directly (one review gate instead of two).

## One-time setup

1. **Create the bot user** (as an admin):
   ```bash
   curl -s -X POST "$STINGRAY_URL/api/users" -H "X-API-Key: $ADMIN_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"username":"claude-bot","display_name":"Claude","email":"claude-bot@localhost","password":"<random>","role":"member"}'
   ```
   Note the returned `id` — that's `RESOLVER_BOT_USER_ID`. A `member` (not admin)
   is correct: it can only modify tickets assigned to it (least privilege).

2. **Mint its API key:**
   ```bash
   curl -s -X POST "$STINGRAY_URL/api/users/<bot_id>/api-keys" -H "X-API-Key: $ADMIN_KEY" \
     -H 'Content-Type: application/json' -d '{"name":"resolver"}'
   ```
   Copy the one-time `api_key` into `STINGRAY_API_KEY`.

3. **Configure and install:**
   ```bash
   cd resolver
   cp .env.example .env          # fill in URL, key, bot id, PROJECTS_ROOT
   python -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
   `STINGRAY_URL` for the resolver should point at the backend directly (no
   `/api` prefix) — e.g. `http://localhost:8000`.

## Filing a "please fix this" ticket

Create a normal ticket with `assigned_to = <claude-bot id>`:

```bash
curl -s -X POST "$STINGRAY_URL/api/tickets" -H "X-API-Key: $YOUR_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "code_review",
    "title": "Fix N+1 query in the activity feed",
    "description": "Listing activity issues one query per row. Please batch it.",
    "priority": "high",
    "assigned_to": <bot_id>,
    "tags": ["repo:ticketing"],
    "code_blocks": [{"filename":"backend/activity.py","language":"python","line_start":10,"line_end":24,"content":"..."}]
  }'
```

- **`tags: ["repo:<name>"]`** selects the target repo — resolves to
  `PROJECTS_ROOT/<name>` (or a `REPO_MAP` override). Required unless `DEFAULT_REPO`
  is set. Anything outside `PROJECTS_ROOT` is rejected.
- **`type: code_review`** keeps your `code_blocks` (they're dropped for `task`).
- Add **`"dangerous"`** to `tags` to skip the plan gate.

## Approving / revising a plan

When the ticket comes back to you (`awaiting-plan-approval`, assigned to you),
read the plan comment, then **re-assign the ticket to `claude-bot`** and add a
comment:

- `/approve` — implement the plan.
- `/revise <notes>` — re-plan with your notes.

## Filing tickets from a run

When a resolver needs to file its *own* Stingray ticket — a review request for the
changes it made, or a follow-up issue it noticed — it uses `file_ticket.py` instead
of hand-writing `curl`. The URL, API key and bot identity come from the resolver's
`.env` (never the agent's prompt), enum/required fields are validated up front, and
`--code-block` reads the exact lines off disk so multi-line code never has to be
JSON-escaped. The implement-phase prompt points the agent at it automatically.

```bash
.venv/bin/python file_ticket.py \
  --type code_review --title "Review: auth refactor" \
  --priority high --tag backend \
  --code-block backend/auth.py:python:60-66      # PATH:LANGUAGE:START-END, read off disk

.venv/bin/python file_ticket.py \
  --type task --title "Flaky retry test" --description "Failed twice this week"
```

- `--type code_review|task`, `--title` are required; `--priority` defaults to `medium`.
- `--tag` and `--code-block` are repeatable; `--assign <user_id>` sets the assignee.
- `--code-block` paths resolve under `--root` (default: the current directory), so run
  it from the repo root and pass repo-relative paths. Only valid for `code_review`.
- `--dry-run` prints the JSON payload without filing.

### The `/ticket` directive (you ask, the resolver files)

You can also ask the resolver to file a ticket **without the LLM in the loop** — write
a `/ticket` line in a bot-assigned ticket's **description or a comment**, and the
resolver parses it deterministically (like `/approve`) and files it via the API on its
next sweep:

```
/ticket <type> "<title>" [--priority low|medium|high|critical] [--tag NAME]...
        [--description "..."] [--assign USER_ID] [--code-block PATH:LANG:START-END]...
```

```
/ticket task "Add index on tickets.created_at" --priority high --tag backend
/ticket code_review "Review the new retry path" --code-block stingray.py:python:80-105
```

- `<type>` is `task` or `code_review`; quote the title. Same validation and on-disk
  `--code-block` reading as `file_ticket.py` (code-block paths are relative to the
  ticket's target repo). Only honored on tickets assigned to this bot.
- **Filed once:** the resolver replies with a `🎫 Filed from /ticket` comment carrying
  each directive's `[key:…]`, and skips keys it has already handled — so a directive
  sitting in the body isn't re-filed every sweep. A malformed directive is reported in
  that comment once, not repeatedly.
- **Default assignee:** the person who wrote the directive (so you can find the ticket
  you asked for); `--assign <user_id>` overrides.

## Running it

```bash
.venv/bin/python resolve_tickets.py            # one sweep
.venv/bin/python resolve_tickets.py --dry-run  # show actions, change nothing
.venv/bin/python resolve_tickets.py --ticket 42  # just ticket 42
```

Cron (under `flock` so sweeps never overlap):

```cron
*/10 * * * * /usr/bin/flock -n /tmp/stingray-resolver.lock \
  /home/penguin/projects/ticketing/resolver/.venv/bin/python \
  /home/penguin/projects/ticketing/resolver/resolve_tickets.py \
  >> /home/penguin/projects/ticketing/resolver/logs/cron.log 2>&1
```

Bound a busy sweep with `--max-tickets N` (or `MAX_TICKETS_PER_SWEEP`) so one
tick does a fixed amount of work under the lock and the next tick continues.

## Scaling: multiple resolvers / identities

The resolver claims work by sweeping tickets **assigned to its own bot user**
(`RESOLVER_BOT_USER_ID`), so the way to run several resolvers is to give each its
own bot user and route tickets by assignment. No shared coordination is needed —
the `flock` only serializes sweeps **on one machine**, and distinct bot ids mean
two resolvers never see the same ticket.

This covers both shapes the homelab might take:

- **2–3 stations:** one bot user per station (`claude-bot-vm1`, `claude-bot-vm2`,
  …); assign a ticket to whichever station should do it.
- **Multiple agents:** a `claude-bot` and a `gemini-bot`, each on its own box (or
  the same one), with `RESOLVER_AGENT` set per resolver — use Claude for the heavy
  lifting and a free/cheap harness (opencode + Gemini) for mechanical tickets.

Each resolver just needs its own `.env` (its `STINGRAY_API_KEY`,
`RESOLVER_BOT_USER_ID`, and `RESOLVER_AGENT`).

### Agents

The plan/implement invocation is behind an `AgentRunner` interface (`agents.py`);
the orchestration around it is agent-agnostic. Two runners ship today: `claude`
(Claude Code) and `opencode` (the [opencode](https://opencode.ai) CLI, which is
model-agnostic — point it at Gemini, etc.). Select one per resolver with
`RESOLVER_AGENT`. Selecting an unregistered agent fails fast at startup with
guidance, so a half-configured resolver never strands a ticket. To add a third
(e.g. OpenAI Codex): subclass `AgentRunner`, implement `run()`, `register_runner(...)`
it (see the `CodexRunner` template).

The agent-invocation env vars are agent-neutral (`AGENT_BIN`, `AGENT_MODEL`,
`AGENT_TIMEOUT`, `AGENT_IMPLEMENT_TIMEOUT`, `AGENT_IMPLEMENT_TOOLS`); the legacy
`CLAUDE_*` names still work as fallbacks, so existing Claude resolvers need no
change.

#### Adding the opencode + Gemini bot (a cheap second bot)

Run a second resolver that drives opencode against Gemini's free API tier, and
route the simple work to it by **assignment** — no dispatcher code, just a second
bot user.

1. **Create `gemini-bot`** (a `member`, exactly like `claude-bot` in setup above)
   and mint its API key. Note its user id.
2. **Install opencode** on that box and give it a Gemini **API key** (from
   [AI Studio](https://aistudio.google.com/apikey)) via opencode's auth/config —
   use the API-key path, *not* the bundled Gemini-CLI login (Google discontinued
   that free OAuth tier on 2026-06-18; the API free tier remains).
3. **Give it its own env file** in this same dir (e.g. `.env.gemini`) — both
   identities share the code; `RESOLVER_ENV_FILE` picks which config to load
   (`.env.*` is gitignored except `.env.example`):
   ```bash
   RESOLVER_AGENT=opencode
   RESOLVER_BOT_USER_ID=<gemini-bot id>
   STINGRAY_API_KEY=<gemini-bot's own key>   # not the admin key
   AGENT_BIN=/home/you/.local/bin/opencode   # absolute path for cron's thin PATH
   AGENT_MODEL=google/gemini-2.5-flash       # provider/model
   # plan phase uses opencode's read-only `plan` agent; implement uses `build`.
   # Override only if you've defined custom opencode agents:
   # OPENCODE_PLAN_AGENT=plan
   # OPENCODE_BUILD_AGENT=build
   ```
4. **Add a second cron line** under its *own* `flock` lock, selecting the env file:
   ```cron
   */10 * * * * /usr/bin/flock -n /tmp/stingray-resolver-gemini.lock \
     /usr/bin/env RESOLVER_ENV_FILE=.env.gemini \
     /home/you/projects/ticketing/resolver/.venv/bin/python \
     /home/you/projects/ticketing/resolver/resolve_tickets.py \
     >> /home/you/projects/ticketing/resolver/logs/cron-gemini.log 2>&1
   ```

**Routing rule:** assign mechanical tickets (small PRs, merge conflicts, lint /
dependency bumps) to `gemini-bot`; assign heavy or ambiguous work to `claude-bot`.
Each resolver only sweeps its own bot's queue, so the two never collide.

> **Read-only safety:** the plan phase runs opencode's permission-restricted
> `plan` agent and does **not** pass `--dangerously-skip-permissions`, so it
> cannot edit files or run shell — the same two-gate guarantee Claude gets. The
> implement phase uses the unrestricted `build` agent; isolation comes from the
> per-ticket worktree + the `PROJECTS_ROOT` allowlist.

## Logging & audit trail

Every sweep writes three kinds of log under `logs/` (everything is scrubbed of
the API key and token-shaped strings first):

| File | Contents |
|------|----------|
| `cron.log` | the INFO summary (sweep start/done, per-ticket phase transitions) |
| `sweep-<ts>.log` | the full human-readable trace at DEBUG — every `git`/`gh`/`bash` command, every API call, every agent tool use |
| `audit-<ts>.jsonl` | one structured JSON object per event (`subprocess` / `api` / `agent_tool` / `phase`) for grep/analysis |
| `ticket-<id>-<phase>-<ts>.log` | the raw agent transcript (Claude stream-json / opencode JSONL) for that run |

Because the implement phase streams the agent's output as JSON, the audit log
records **each file the bot reads/writes/edits and each shell command it runs**
(the `agent_tool` events carry an `agent` field — `claude` or `opencode`), e.g.:

```bash
# what did the bot touch on ticket 42?
grep '"kind": "agent_tool"' logs/audit-*.jsonl | grep '"ticket": "42"'
# every non-zero subprocess in the last sweep
jq 'select(.kind=="subprocess" and .rc!=0)' logs/audit-*.jsonl
```

### Viewing logs

Use `./logs.py` to read the per-ticket plan/implement transcripts without
hunting for timestamped filenames. It covers **both** bots (they share `logs/`)
and reads transparently from the daily archives once a day has been rolled up:

```bash
./logs.py            # list recent runs, newest first (id, phase, time, size, status)
./logs.py 42         # print ticket 42's latest *implement* transcript
./logs.py 42 --plan  # ...its plan transcript instead
./logs.py 42 --all   # list every run for ticket 42
./logs.py 42 -f      # follow a run in progress (tail -f)
```

### Retention, archiving & rotation

With two bots sweeping every 10 min, the noisy bit is the per-sweep files, so the
lifecycle (all at sweep start) keeps `logs/` small without losing history:

- **Empty sweeps leave nothing.** A sweep that processed no tickets deletes its
  own `sweep-*`/`audit-*` pair, so ~288 idle sweeps/day don't pile up.
- **Finished days are batched & compressed.** Loose logs from days older than
  `LOG_ARCHIVE_AFTER_DAYS` (default 1 — today stays loose) are rolled into one
  `logs/archive/<date>.tar.gz` per day (gzip; ~20–50× smaller). `./logs.py` and
  `tar`/`zcat` read them back.
- **Old archives are deleted** past `LOG_RETENTION_DAYS` (default 14).

Archiving is `flock`-guarded so the two bots sharing `logs/` never double-roll.

`cron.log` is append-only; set **`CRON_LOG`** in each bot's env to its own file
(`logs/cron.log` for the Claude bot, `logs/cron-gemini.log` for the Gemini bot)
and the resolver size-rotates it to `<file>.1` at sweep start (cap
`CRON_LOG_MAX_BYTES`, default 5 MB) — no external logrotate needed. (Prefer
logrotate? Leave `CRON_LOG` unset and add a stanza with `copytruncate`.)

## Reliability

- **API retries:** transient Stingray failures (connection reset, timeout, HTTP
  429/500/502/503/504) are retried with exponential backoff (`STINGRAY_MAX_RETRIES`,
  honoring `Retry-After`), so a network blip mid-sweep can't strand a ticket in a
  `claude:*` in-flight state.
- **No false successes:** `git push` / `gh pr create` exit codes are checked — a
  failed push hands the ticket back re-implementable with the error instead of
  posting an "Implemented" comment with no PR link.
- **Attempt cap:** a ticket that keeps failing the same phase is auto-retried at
  most `MAX_ATTEMPTS` times (default 3), then reopened and handed to a human so it
  stops burning tokens every tick. The streak resets once the resolver makes
  progress (posts a plan or a PR).
- **Reviewer feedback:** when a PR gets `changes_requested`, the reviewer's note
  is threaded into the rework prompt so the bot isn't re-implementing blind.

## Output modes (auto-detected per repo)

| Condition | Result |
|-----------|--------|
| `origin` remote + `gh auth status` OK | pushes branch, opens a PR, links it on the ticket |
| no remote / no `gh` | keeps a local branch `claude/ticket-<id>`, reports it |
| `PATCH_FALLBACK=1` | posts the diff as a comment, writes nothing persistent |

Auto-merge is never done — Claude only opens the PR; you approve and merge.

## How it stays safe

- **Repo allowlist:** every target is `realpath`-resolved and must live inside
  `PROJECTS_ROOT`; traversal/symlink escapes are rejected (`config.py`).
- **Isolated worktrees:** edits happen in `resolver/work/ticket-<id>`, removed
  after each run; your main checkout and branch are untouched.
- **Read-only planning:** the plan phase grants only `Read/Glob/Grep` (no
  Edit/Write/Bash), so it cannot change anything; the implement phase gets the
  tools in `CLAUDE_IMPLEMENT_TOOLS`.
- **Least-privilege bot:** a `member` user that can only act on its own tickets.
