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
- **Multiple agents:** a `claude-bot` and a `codex-bot`, each on its own box (or
  the same one), with `RESOLVER_AGENT` set per resolver.

Each resolver just needs its own `.env` (its `STINGRAY_API_KEY`,
`RESOLVER_BOT_USER_ID`, and `RESOLVER_AGENT`).

### Agents

The plan/implement invocation is behind an `AgentRunner` interface (`agents.py`);
the orchestration around it is agent-agnostic. `claude` (Claude Code) is
implemented and registered. To add another (e.g. OpenAI Codex): subclass
`AgentRunner`, implement `run()` to drive that CLI, `register_runner(...)` it
(see the `CodexRunner` template), then point a resolver at it with
`RESOLVER_AGENT=<name>`. Selecting an unregistered agent fails fast at startup
with guidance, so a half-configured resolver never strands a ticket.

## Logging & audit trail

Every sweep writes three kinds of log under `logs/` (everything is scrubbed of
the API key and token-shaped strings first):

| File | Contents |
|------|----------|
| `cron.log` | the INFO summary (sweep start/done, per-ticket phase transitions) |
| `sweep-<ts>.log` | the full human-readable trace at DEBUG — every `git`/`gh`/`bash` command, every API call, every Claude tool use |
| `audit-<ts>.jsonl` | one structured JSON object per event (`subprocess` / `api` / `claude_tool` / `phase`) for grep/analysis |
| `ticket-<id>-<phase>-<ts>.log` | the raw Claude stream-json transcript for that run |

Because the implement phase runs Claude with `--output-format stream-json`, the
audit log records **each file the bot reads/writes/edits and each shell command
it runs**, e.g.:

```bash
# what did the bot touch on ticket 42?
grep '"kind": "claude_tool"' logs/audit-*.jsonl | grep '"ticket": "42"'
# every non-zero subprocess in the last sweep
jq 'select(.kind=="subprocess" and .rc!=0)' logs/audit-*.jsonl
```

Logs older than `LOG_RETENTION_DAYS` (default 14) are pruned at the start of each
sweep. `cron.log` is append-only and not pruned — rotate it with logrotate:

```
/home/penguin/projects/ticketing/resolver/logs/cron.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
```

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
- **Reserved control tags:** the bot drives its workflow through control tags
  (`claude:*`, `repo:*`, `dangerous`, `fix`). The **backend** restricts setting
  these to admins and the resolver bot, so an ordinary user can't hijack the
  automation (re-point a repo, force an "implement & open PR" phase, or strip
  the `dangerous` gate) via the UI or their own API key. For this to recognize
  the bot, set **`RESOLVER_BOT_USER_ID` in the backend's environment** to the
  bot's user id (the same id the resolver uses) — otherwise only admins can
  manage these tags and the bot's `set_state` transitions will be rejected.
  (Alternatively, promote the bot to `admin` and leave `RESOLVER_BOT_USER_ID`
  unset on the backend; the env-var approach keeps the bot least-privilege.)
