# Stingray ticket resolver

A standalone companion tool that lets you **hand a ticket to a Claude Code
instance**. You file a ticket assigned to a dedicated `claude-bot` user; the
resolver (run on a cron sweep) picks it up, **proposes a plan**, waits for your
`/approve`, then **implements the fix on a branch and opens a PR** — leaving the
plan, the summary, and every status change in the ticket's trail.

It talks only to the existing Stingray REST API (see `../api_guide.md`) — no
backend changes. All code execution happens in an isolated `git worktree`, so
your live checkouts under `PROJECTS_ROOT` are never touched.

> **This is an optional, advanced add-on.** The core Stingray ticketing app runs
> fine without it. Enable the resolver only if you want tickets resolved by an AI
> agent.

## Prerequisites

Before you start, you need:

- **A coding-agent CLI on the resolver host.** Built-in support: [Claude Code]
  (`claude`) or [opencode] (model-agnostic, e.g. Gemini). Set which one with
  `RESOLVER_AGENT`. Register others in `agents.py`.
- **Provider API keys / credentials for that CLI**, configured the way the CLI
  expects (e.g. logged into Claude Code, or an `opencode` provider key). **These
  are yours to supply, and agent runs cost money** — the resolver does not manage
  billing or keys.
- **`git` and (for PRs) the `gh` CLI authenticated** on the host, plus the repos
  you want it to touch checked out under `PROJECTS_ROOT`.
- **Python 3.12+.**

[Claude Code]: https://docs.claude.com/en/docs/claude-code
[opencode]: https://opencode.ai

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

### Recommended: automatic provisioning

The root [`install.sh`](../install.sh) can do all of this for you. With
`SEED_RESOLVER_BOT=true` (the installer sets it), the backend seeds the bot user,
mints its API key, and the installer writes `resolver/.env` for you. Then just
build the venv:

```bash
cd resolver
./setup.sh                       # creates .venv, installs deps, sanity-checks .env
```

The seeded bot is flagged `is_resolver_bot` in the database, so it can manage the
reserved control tags **without** you matching any `RESOLVER_BOT_USER_ID` between
the backend and the resolver.

### Manual alternative

If you're attaching a resolver to an existing instance (no fresh seed), create the
bot yourself:

1. **Create the bot user** (as an admin):
   ```bash
   curl -s -X POST "$STINGRAY_URL/api/users" -H "X-API-Key: $ADMIN_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"username":"claude-bot","display_name":"Claude","email":"claude-bot@localhost","password":"<random>","role":"member"}'
   ```
   Note the returned `id` — that's `RESOLVER_BOT_USER_ID`. A `member` (not admin)
   is correct: it can only modify tickets assigned to it (least privilege). For it
   to manage control tags, either set the backend's `RESOLVER_BOT_USER_ID` to this
   id, or set the user's `is_resolver_bot` flag.

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
   ./setup.sh                    # or: python -m venv .venv && .venv/bin/pip install -r requirements.txt
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
  is set. Anything outside `PROJECTS_ROOT` is rejected. Easy to forget when filing by
  hand, and without it the resolver can only review embedded code blocks (never fix
  them) — `file_ticket.py` fills it in from the checkout you run it in, so prefer that
  over `curl`.
- **`type: code_review`** keeps your `code_blocks` (they're dropped for `task`).
- Add **`"dangerous"`** to `tags` to skip the plan gate.

## Approving / revising a plan

When the ticket comes back to you (`awaiting-plan-approval`, assigned to you),
read the plan comment, then **re-assign the ticket to `claude-bot`** and add a
comment:

- `/approve` — implement the plan.
- `/revise <notes>` — re-plan with your notes.

## Reviewing code (review mode)

A ticket of **type `code_review`** assigned to the bot is *reviewed*, not planned/
implemented: the resolver runs the agent **read-only**, reviews the code, and posts its
findings (issues / risks / suggestions, grouped by severity) as a `🔎 Code review`
comment — no PR, no edits — then hands the ticket back to you. This is how a bot
resolves a review request another bot (or you) filed.

- **What it reviews:** the ticket's `code_blocks` if present, otherwise it explores the
  repo (selected by the `repo:<name>` tag) to find the code your description refers to —
  so you can just file *"Review program installation in repman"* without attaching code.
- **Reviewed once:** a re-swept review ticket isn't re-reviewed unless a human comment
  says `/review`.
- **Also fix it:** add the **`fix`** tag to a `code_review` ticket and, after reviewing,
  the resolver treats its findings as a plan and routes into the normal `/approve` →
  implement → PR gate (or applies them straight away if the ticket is also `dangerous`).
  Without `fix`, review mode is strictly findings-only.
- **Fix it afterwards (`/fix`):** a findings-only review leaves the ticket tagged
  `resolver:awaiting-fix`, which keeps it *actionable* — no follow-up ticket needed.
  Re-assign it to the bot with a **`/fix`** comment (or `/fix <notes>` to steer which
  findings to apply, or just add the `fix` tag and re-assign) and the resolver replays
  the findings it posted as the implement plan and opens a PR. The **Apply fixes**
  button on the ticket page does both steps in one click. Applying needs a checkout:
  a review with no `repo:<name>` tag says so instead of implementing.

  ```
  file ticket ──▶ review ──▶ 🔎 findings + resolver:awaiting-fix ──▶ /fix ──▶ PR
                                        ▲                              │
                                        └────── /review (re-review) ───┘
  ```

View a review transcript with `./logs.py <id> --review`.

## Filing tickets from a run

When a resolver needs to file its *own* Stingray ticket — a review request for the
changes it made, or a follow-up issue it noticed — it uses `file_ticket.py` instead
of hand-writing `curl`. The URL, API key and bot identity come from the resolver's
`.env` (never the agent's prompt), enum/required fields are validated up front, and
`--code-block` reads the exact lines off disk so multi-line code never has to be
JSON-escaped. The implement-phase prompt points the agent at it automatically.

> **Where the code lives now.** The payload/code-block/repo-tag helpers and the REST
> client moved to `cli/stingray_client/`, shared with the `stingray` CLI. `file_ticket.py`
> keeps its full argparse surface (including `--parent` delegation and the
> `RESOLVER_MAX_DELEGATIONS` cap, which are resolver policy) and adapts to the library;
> `stingray.py` subclasses the shared client to add this project's audit logging. The
> resolver installs the library via `-e ../cli` in `requirements.txt`. Nothing about the
> commands below changed.

```bash
.venv/bin/python file_ticket.py \
  --type code_review --title "Review: auth refactor" \
  --priority high --tag backend \
  --code-block backend/auth.py:python:60-66      # PATH:LANGUAGE:START-END, read off disk
  # repo:<name> is added automatically from the git checkout at --root;
  # override with --repo NAME, or suppress it with --no-repo

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
  you asked for). `--assign <user_id>` overrides, and takes a **numeric user id** — a
  username (e.g. `admin`) can't be resolved here because the resolver runs as a
  non-admin bot. Omit `--assign` to assign the new ticket to yourself.
- **Pure filing requests skip planning:** if a bot-assigned ticket's body is *only*
  `/ticket` line(s), there's nothing to implement — the resolver files the directive(s)
  and hands the ticket back to you (`in_review`) instead of spending an agent run
  planning it. Put `/ticket` in a comment (or alongside real content) if you want the
  host ticket worked on too.

## Standard commands (premade prompts)

For tasks you want run the same way across **all** your projects — a security audit
is the canonical example — the resolver ships a library of named, premade prompts.
Invoke one by putting a single slash-command line in a bot-assigned ticket's
**description or a comment**:

```
/security-audit

Focus on the new auth router.
```

When the resolver sees `/security-audit`, it loads `commands/security-audit.md`,
injects its body as the ticket's **primary objective**, and runs the normal
lifecycle. The ticket's own title/description (the "Focus on…" line) are kept as
supporting context. Detection is deterministic — the model never decides which
command ran — mirroring the `/ticket` directive above.

- **One library, every project.** Commands live in `resolver/commands/*.md`, so a
  recurring cross-project task is defined once. The seeded set includes
  `/security-audit`, `/dependency-audit`, `/test-coverage`, and `/scaffold`.
- **`type` controls routing.** A command's frontmatter `type: code_review` runs the
  read-only review lifecycle (findings posted, no PR) — even on a ticket whose own
  type is `task`. `type: task` (the default) runs plan → approve → implement.
- **Composes with `delegate`.** A ticket tagged `delegate` that also invokes a
  command (e.g. `/security-audit` + `delegate`) has the lead resolver audit the repo
  using the premade prompt, then fan out one fix sub-task per finding to other
  resolvers (see "Delegation / fan-out"). The command's body drives the audit; the
  sub-tasks are filed as `task` tickets regardless of the command's `type`.
- **Unknown command?** If a `/foo` line matches no template, the resolver posts a
  one-time comment listing the available commands and handles the ticket normally.
- **Authoring:** see `commands/README.md` for the file format. Drop in a new
  `<name>.md` and it's picked up on the next sweep.

`/ticket`, `/approve`, `/revise`, and `/review` are reserved control verbs and are
never treated as standard commands.

### `/scaffold` — a guided exercise out of an existing repo

`/scaffold` inverts what the resolver normally does: instead of implementing a
ticket, it writes the **skeleton** of a feature and hands the work back as a
backlog for a human to fill in. It is the existing-codebase counterpart to
`stingray scaffold --guided`, which does the same thing for an empty directory.

```
/scaffold add a payments module

Card charges and webhook handling. Third-year coursework —
requirements only, leave the architecture open.
```

The lifecycle is the ordinary `task` one, which is the point: it plans first, so
you see the proposed skeleton and `/approve` (or `/revise`) it before any code
exists. The implement run then:

1. writes stubs — `STINGRAY-STUB:` / `ACCEPTANCE:` plus a `NotImplementedError`
   — with everything around them (signatures, imports, routes, wiring) real and
   correct, so the tree still imports and the existing tests still pass;
2. writes an `ASSIGNMENT.md` handout (learning goals, milestones, rubric) and
   gitignores it;
3. opens a PR of the skeleton, as usual.

Then `resolver/scaffold_followup.py` finishes the job deterministically:

- The handout is **lifted out of the worktree before the commit** and posted as a
  comment on the ticket. `git add -A` honours `.gitignore`, so a correctly-ignored
  handout could never reach anyone via the PR — the comment is how it is
  delivered.
- The finished tree is **scanned** for `STINGRAY-STUB` markers, restricted to the
  files the run touched, and one ticket is filed per marker. Scanning rather than
  scraping `created ticket #N` out of the agent log (the way delegation does) is
  what guarantees every marker gets exactly one ticket over a ten-plus-stub
  backlog.
- Children carry `epic:<this ticket's id>` and **never** `parent:` — `parent:`
  makes a child self-driving, and a learner's exercise being auto-implemented by
  a bot defeats the entire feature. The scaffold ticket is itself the epic; no
  second epic is created.
- A re-run (a rework after review) rebuilds the skeleton but does **not** refile
  the backlog — those tickets may already have been worked on.

Capped at 30 exercise tickets per run; over that the roll-up comment says how many
were left untracked.

## The `resolver` CLI (one-stop)

`cli.py` is a single front door over the scripts below — creating/managing bots,
running sweeps, the worker roster, logs, and cost stats:

```bash
.venv/bin/python cli.py bot create open-bot --desc "cheap mechanical fixes"
.venv/bin/python cli.py bot list             # all resolver bots + their .env files
.venv/bin/python cli.py roster               # build a RESOLVER_WORKERS string
.venv/bin/python cli.py run --env .env.open --dry-run
.venv/bin/python cli.py stats --ticket 42    # token usage + cost (incl. children)
.venv/bin/python cli.py logs 42 --all        # wraps logs.py
.venv/bin/python cli.py file --type task --title "…"   # wraps file_ticket.py
```

`bot create` provisions the bot **and** mints its key in one admin call (the
`POST /users/resolver-bot` endpoint), then writes a ready-to-run `.env.<name>` —
no hand-syncing `RESOLVER_BOT_USER_ID`. It needs an admin key via `--admin-key`
or `$STINGRAY_ADMIN_KEY`; everything else uses a resolver `.env` identity.

## Running it

```bash
.venv/bin/python resolve_tickets.py            # one sweep
.venv/bin/python resolve_tickets.py --dry-run  # show actions, change nothing
.venv/bin/python resolve_tickets.py --ticket 42  # just ticket 42
```

**Cron** (under `flock` so sweeps never overlap). Set `RESOLVER_DIR` to wherever
you checked the repo out — no absolute paths to hand-edit:

```cron
RESOLVER_DIR=/opt/ticketing/resolver
*/10 * * * * /usr/bin/flock -n /tmp/stingray-resolver.lock \
  $RESOLVER_DIR/.venv/bin/python $RESOLVER_DIR/resolve_tickets.py \
  >> $RESOLVER_DIR/logs/cron.log 2>&1
```

**systemd** (a service + timer that does the same thing) — copy the templates and
fill in the two `User=`/`WorkingDirectory=` placeholders:

```bash
sed "s#/opt/ticketing/resolver#$(pwd)#" stingray-resolver.service \
  | sudo tee /etc/systemd/system/stingray-resolver.service
sudo cp stingray-resolver.timer /etc/systemd/system/
sudo systemctl enable --now stingray-resolver.timer
```

See [`stingray-resolver.service`](./stingray-resolver.service) and
[`stingray-resolver.timer`](./stingray-resolver.timer).

Bound a busy sweep with `--max-tickets N` (or `MAX_TICKETS_PER_SWEEP`) so one
tick does a fixed amount of work under the lock and the next tick continues.

## Daily digest (optional)

Everything above is **reactive**: the resolver works whatever was assigned to its
bot. `digest.py` is the other half — a scheduled pass that looks at the backlog *as
a whole* and files one **report ticket**: a short summary paragraph over a markdown
checklist of every ticket it covered.

```bash
cp digests.example.toml digests.toml     # then edit
.venv/bin/python digest.py --list        # what's configured
.venv/bin/python digest.py --name daily --dry-run   # render to stdout, file nothing
.venv/bin/python digest.py --name daily             # file it
resolver digest --name daily                        # same, via the CLI
```

Nothing auto-runs off a digest. It is a report for a human; acting on a line means
opening that ticket and working it the usual way.

### Configuring digests

Each `[[digest]]` block in `digests.toml` is one named report. The `query` field is
a `GET /tickets` query string — **the exact string a saved view stores**, so you can
build a filter in the UI, save it, and paste it in:

```toml
[[digest]]
name      = "daily"
title     = "Daily digest — {date}"
query     = "sort=priority&order=desc"
statuses  = ["open", "in_review", "changes_requested"]
assign_to = 1          # user id who receives the report
sections  = ["overdue", "high-priority", "awaiting-fix", "stale", "unassigned"]
exclude_tags = ["stub"]
```

`GET /tickets` takes **one** status, but a digest cares about several — the
resolver parks a reviewed ticket at `in_review`, so a digest pinned to
`status=open` could never show an `awaiting-fix` or `awaiting-pr-review` section
at all. `statuses` fans the same query out over each one and merges the results
(re-sorting by urgency, since the merge destroys the server's ordering).

`exclude_tags` drops tickets client-side. `stub` is in the shipped default because
a guided-project scaffold files one exercise ticket per stub — a coursework
backlog, not work waiting to be picked up; left in, a few hundred of them bury
every real signal.

An unsupported query param is rejected **at load time** rather than ignored by the
server — a typo'd filter would otherwise silently produce a digest of the wrong set
of tickets. `digests.toml` is gitignored (it carries user ids and repo scopes);
`digests.example.toml` documents every field.

Each ticket lands in the **first** section it matches, in the order you list them,
so the checklist reads as a to-do list instead of repeating one ticket under four
headings. Available sections: `overdue`, `high-priority`, `awaiting-fix`,
`awaiting-plan-approval`, `awaiting-pr-review`, `stale`, `unassigned`, `new`,
`recently-resolved`.

Note `stale` outranks `unassigned` by default: "untouched for 70 days" is a
stronger signal than "nobody owns it", and in a tracker where little is ever
assigned, an `unassigned` section placed first swallows the whole backlog. Two
caps keep the report readable — `max_tickets` (whole run) and `max_per_section`,
so one big pile can't bury the rest; both report what they trimmed.

### `DIGEST_ADMIN_KEY` is required, and must really be an admin's

The API shows non-admins **only tickets they created or are assigned to**. The
resolver's own key is a least-privilege member, so running the digest on it would
quietly report on the bot's queue rather than the backlog — a digest covering a
fraction of the tracker is worse than none. `digest.py` therefore refuses to start
without `DIGEST_ADMIN_KEY`, and warns loudly if that key turns out to belong to a
non-admin. Mint one as an admin from Profile → API keys.

The summary paragraph comes from one OpenAI-compatible chat completion
(`DIGEST_API_*`, falling back to `REVIEW_API_*`). It is optional in the strong
sense: with no endpoint configured, or on a 429, the digest still files — you lose
the paragraph, not the report. **The checklist is always derived from the query
results, never from model output**, and the model is told not to emit ticket
numbers, so the prose can never contradict or invent a line.

### Against an instance hosted elsewhere

The digest only ever talks to the API — it checks nothing out and runs no agent — so it
does **not** need to live on a resolver host, or near your repositories, or on the machine
running Stingray. It needs network access and two settings:

```bash
STINGRAY_URL=https://tickets.example.com/api
DIGEST_ADMIN_KEY=sk_...
```

That is the whole config. `Config.load(api_only=True)` relaxes the three requirements
that exist for the sweep (`STINGRAY_API_KEY`, `RESOLVER_BOT_USER_ID`, `PROJECTS_ROOT`),
so a box whose only job is filing a digest doesn't have to invent a bot id and an empty
directory to satisfy checks nothing on its path reads.

Three things to get right for a remote instance:

- **The URL is the API base, so it ends in `/api`** on the default deployment (nginx
  serves the SPA at the root and proxies `/api`). Point it at the root and every call
  returns the web page; the client catches this and says so rather than surfacing a JSON
  decode error. A backend you reach directly needs no suffix.
- **Mint `DIGEST_ADMIN_KEY` on that instance**, as an admin, from **Profile → API keys**.
  Keys are per-instance — a key from your local server is meaningless remotely. Give it a
  name you'll recognise (`digest-cron`) so it can be revoked on its own, and consider an
  expiry.
- **Use HTTPS.** The key travels in an `X-API-Key` header on every request.

The env file is optional: `_load_env_file` ignores a missing path and falls through to the
real environment, so a container or a systemd unit can inject both values without one:

```bash
docker run --rm \
  -e STINGRAY_URL=https://tickets.example.com/api \
  -e DIGEST_ADMIN_KEY \
  -v "$PWD/digests.toml:/app/digests.toml:ro" \
  <your-image> python digest.py --name daily
```

For systemd, keep the key out of the unit file with
`EnvironmentFile=/etc/stingray-digest.env` (mode 0600) rather than `Environment=`, which
is world-readable via `systemctl show`.

Two failure modes worth knowing, because both are quiet rather than loud:

- A key that isn't an admin's still works — the API just shows it only the tickets it
  created or is assigned to, so you get a digest of a slice. The run warns loudly and
  names this; don't ignore it.
- The prose model is optional, so a wrong/expired `DIGEST_API_KEY` costs you the summary
  paragraph and files the report anyway. The reason lands in the log, not the ticket.

Verify a new deployment with a dry run before scheduling anything — it renders to stdout
and files nothing:

```bash
STINGRAY_URL=https://tickets.example.com/api DIGEST_ADMIN_KEY=sk_... \
  python digest.py --name daily --dry-run
```

### Scheduling

Cadence comes from the schedule, not the config file — one entry per digest name.
Cron, matching the resolver's pattern:

```cron
RESOLVER_DIR=/opt/ticketing/resolver
15 8 * * * /usr/bin/flock -n /tmp/stingray-digest.lock \
  $RESOLVER_DIR/.venv/bin/python $RESOLVER_DIR/digest.py --name daily \
  >> $RESOLVER_DIR/logs/cron-digest.log 2>&1
```

Or systemd — see [`stingray-digest.service`](./stingray-digest.service) and
[`stingray-digest.timer`](./stingray-digest.timer). A digest files **at most once
per name per day** (it checks for its own `digest:<name>:<date>` tag first), so a
timer that fires twice, or a manual re-run, is harmless; `--force` overrides.

## Scaling: multiple resolvers / identities

The resolver claims work by sweeping tickets **assigned to its own bot user**
(`RESOLVER_BOT_USER_ID`), so the way to run several resolvers is to give each its
own bot user and route tickets by assignment. No shared coordination is needed —
the `flock` only serializes sweeps **on one machine**, and distinct bot ids mean
two resolvers never see the same ticket.

This covers both common shapes:

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
   AGENT_MODEL=google/gemini-2.5-flash       # provider/model (primary)
   AGENT_FALLBACK_MODELS=google/gemini-2.5-flash-lite  # tried in order after primary
   # plan phase uses opencode's read-only `plan` agent; implement uses `build`.
   # Override only if you've defined custom opencode agents:
   # OPENCODE_PLAN_AGENT=plan
   # OPENCODE_BUILD_AGENT=build
   ```
   Pick a model that's actually good at agentic tool-calling: `gemini-2.5-flash`
   or stronger. `gemini-2.5-flash-lite` stalls on multi-step edits (≈148s/step,
   the loop dies with no changes) and is a poor fit for the implement phase.

   **Reliability — chain across providers, not one project's quota.** A single
   provider's free tier rate-limits hard (HTTP 429); the resolver detects the 429
   and fails over to the next model in `AGENT_FALLBACK_MODELS`, but models inside
   *one* Google project share *one* quota and 429 together. Point the chain at
   **different providers** (each `opencode auth login`'d, independent free quotas):
   ```bash
   AGENT_MODEL=mistral/codestral-latest
   AGENT_FALLBACK_MODELS=openrouter/deepseek/deepseek-chat-v3:free,google/gemini-2.5-flash
   ```
   (`gemini-2.5-pro` is free-tier `limit:0` — it 429s every time and the SDK retries
   it into a fake "hang"; only add it once the key has billing.)

   > ⚠️ **The opencode *agent* needs a high token-per-minute (TPM) budget.** opencode
   > injects a ~32k-token system prompt (tool schemas + skills) on *every* call, so
   > the **agent** (plan/implement) phases are unusable on low-TPM free tiers — e.g.
   > **Groq free** caps TPM at 6–12k, so every `groq/*` model 429s with
   > `ContextOverflowError` before it starts (and `groq/compound` errors outright).
   > Use such providers for **single-shot reviews** instead (below) — those send only
   > the small review prompt. For the agent path, pick a provider with a large/free
   > context budget (Gemini, Mistral, OpenRouter free models) or a paid tier.
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

**Automatic escalation (free → Claude).** So you can route *everything* to the free
bot and let it triage, set `ESCALATE_TO_USER_ID=<claude-bot id>` in `.env.gemini`:
the free bot then reassigns any ticket that is high/critical priority (configurable
via `ESCALATE_PRIORITIES`), tagged `dangerous`, or tagged `claude`, to the Claude
bot — which picks it up on its next sweep. The Claude resolver leaves the var unset,
so it never escalates to itself. **Delegated sub-tasks are exempt:** a ticket with a
`parent:<id>` tag was already routed to this resolver by a lead, so it's never
escalated — its `dangerous` tag there only means "skip the plan gate", and clawing it
back would defeat the lead's capability routing.

**Single-shot reviews (optional, more reliable).** A code review needs no tools — the
ticket carries the `code_blocks`. Set `REVIEW_API_URL` / `REVIEW_API_KEY` /
`REVIEW_API_MODEL` (any OpenAI-compatible `/chat/completions` endpoint — Groq,
Mistral, OpenRouter) and reviews run as a single chat completion instead of the
opencode agent loop, sidestepping the loop's fragility. Unset = review via the agent.

## Delegation / fan-out (optional)

A **lead** resolver can decompose one ticket into independent sub-tasks and hand each
to whichever resolver fits — heavy refactors to the Claude bot, cheap mechanical fixes
to the free bot. You assign the audit once; the resolvers do the rest, and you review
the resulting PRs.

**Strictly opt-in, gated two ways.** Nothing fans out unless *both*:

1. `RESOLVER_ALLOW_DELEGATION=1` on the lead resolver, with a worker roster:
   ```bash
   RESOLVER_ALLOW_DELEGATION=1
   # id:name:desc  — semicolon-separated. The lead agent routes by these blurbs.
   RESOLVER_WORKERS=2:claude:heavy refactors & multi-file changes;3:open:cheap mechanical single-file fixes
   RESOLVER_MAX_DELEGATIONS=10   # hard cap on sub-tasks per run
   ```
2. the ticket carries the reserved `delegate` tag (only you/an admin or a resolver bot
   can set it).

**What happens.** The lead audits the repo read-only, then files one sub-task per issue
via `file_ticket.py --type task --assign <id> --parent <this-id>`, posts a roll-up on
the parent listing each child + assignee, and hands the parent back to you. `--parent`
links the child `parent:<id>` and makes it **self-driving**, with a safety gate:

- the assignee **plans** the child like any ticket, then its **review AI auto-approves**
  the plan and it implements + opens a PR — no human `/approve`, but never an
  unreviewed change. This needs the [plan-critique gate](#plan-critique-gate-optional)
  (`CRITIQUE_API_*`) configured on the worker.
- if the review AI keeps flagging the plan (still `REVISE` after `CRITIQUE_MAX_REVISIONS`),
  the child is handed to the review owner for an explicit `/approve` or `/revise` instead
  of implementing a contested plan unattended.
- **fallback:** with no review AI configured the child runs in `dangerous` mode (implements
  with no plan), the legacy behavior — so delegation still works out of the box, just
  without the auto-approval safety net.

The autonomy is keyed off the reserved `parent:<id>` tag (which only a trusted bot/admin
can set), not a user-settable flag, so a member can't make their own ticket self-driving.

**Safe by construction.** Children are **one level only** — a delegated sub-task may
never carry `delegate`, so it can't fan out again (enforced in `file_ticket.py`). Each
child works on its own `claude/ticket-<id>` branch and opens a PR; nothing is committed
to your default branch or merged automatically. Each finished child PR is reassigned to
**you** (the parent's creator), not the lead bot, so you get the "assigned to you" mail —
the review owner is baked into each child as a `review-by:<id>` tag at creation, because
the worker that finishes the child can't read the parent (ticket read access is narrow).
The per-run cap bounds cost. Delegation needs a Bash-capable runner (the Claude bot);
the opencode `plan` agent has no shell, so an opencode lead can't file sub-tasks.

## Plan-critique gate (optional)

The plan phase trusts the planner to produce a complete, implementable plan; a weak plan
(wrong files, vague steps, a misread requirement) isn't caught until the expensive
implement run has already burned a strong model on it. Set `CRITIQUE_API_URL` /
`CRITIQUE_API_KEY` / `CRITIQUE_API_MODEL` (any OpenAI-compatible `/chat/completions`
endpoint — point it at a cheap, fast model) and a **cheap model vets each freshly
produced plan before the human sees it**. It answers `VERDICT: APPROVE` or
`VERDICT: REVISE`; on REVISE the planner is re-invoked with the critique's notes, up to
`CRITIQUE_MAX_REVISIONS` times (default 1), and the verdict is appended to the plan
comment so the reviewer sees it. It runs **inside the plan phase**, before the
`/approve` hand-back — no extra ticket state.

It is **fail-open**: a quota'd, flaky, or unparseable critique never blocks a plan that
was actually produced (the resolver proceeds with the plan it has). Blank `CRITIQUE_API_*`
= gate off (plans go straight to the human — the legacy behavior). Each REVISE costs an
extra (cheap) critique call plus a full re-plan agent run, so keep `CRITIQUE_MAX_REVISIONS`
small. The critique's token usage is recorded both as a `token_usage` audit event and as
a first-class `AgentRun` (`agent=critique-api`, `phase=plan-critique`), so its cost shows
on the ticket like any other phase.

## Verification gate (optional)

The implement phase trusts the agent to run tests. To verify independently, set
`VERIFY_COMMAND` — a shell command the resolver runs **in the worktree** after the
implement run. If it fails, the resolver feeds the output back and re-invokes the agent
to repair, in-process, up to `VERIFY_MAX_RETRIES` times (default 1). If it still fails,
the PR / hand-back is published anyway but prefixed with a **⚠️ Tests failing** banner
(in both the comment and PR body) so a human takes over — work is never discarded. Blank
`VERIFY_COMMAND` = gate off (publish as soon as there's a diff). `VERIFY_TIMEOUT`
(default 900s) bounds each run.

> **Worktree-env gotcha:** the gate runs in a fresh `git worktree`, which does **not**
> contain gitignored artifacts like `.venv` or `node_modules`. Make the command
> self-contained — an absolute interpreter path, `uv run`, or a venv bootstrap. A
> command that can't find its environment just fails verification and surfaces as a
> flagged publish. Example:
> `VERIFY_COMMAND=cd backend && /abs/.venv/bin/pytest -q --rootdir=$PWD -p no:cacheprovider`
>
> A repair run is a full agent run, so each retry costs tokens/time; the default of 1
> keeps it bounded. Repair runs are subject to the same worktree-escape hard-stop as the
> first run.

> **Read-only safety:** the plan phase runs opencode's permission-restricted
> `plan` agent and does **not** pass `--dangerously-skip-permissions`, so it
> cannot edit files or run shell — the same two-gate guarantee Claude gets. The
> implement phase uses the unrestricted `build` agent; isolation comes from the
> per-ticket worktree + the `PROJECTS_ROOT` allowlist.

> **Worktree anchoring:** the runner passes `--dir <worktree>` to `opencode run`.
> Without it opencode roots in its *global* project (`$HOME`) rather than the
> per-ticket checkout, so the agent explores/edits the wrong tree and the implement
> diff comes back empty ("produced no code changes").

> **Transient-failure handling:** Gemini sometimes returns an overloaded-model 503
> that opencode swallows (exits 0 with no output). The runner detects that, retries
> the primary model once with backoff, then escalates to `AGENT_FALLBACK_MODEL`
> for that run, so one provider blip doesn't burn a whole ticket attempt. Each
> attempt keeps its own `…-try<N>.log`.

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
  after each run; your main checkout and branch are untouched. The implement run
  is *anchored* to the worktree (cwd/`--dir`), approved-plan paths are reanchored
  from the main checkout to the worktree, and the runner warns if a run dirties the
  main checkout. Note this is anchoring, **not** a hard sandbox: the implement
  phase allows broad `Bash`, so a sufficiently determined/confused agent could still
  `cd` elsewhere. For untrusted inputs, run the resolver as a least-privileged user
  (or in a container) whose filesystem write access is limited to the repo.
- **Read-only planning:** the plan phase grants only `Read/Glob/Grep` (no
  Edit/Write/Bash), so it cannot change anything; the implement phase gets the
  tools in `CLAUDE_IMPLEMENT_TOOLS`.
- **Least-privilege bot:** a `member` user that can only act on its own tickets.
- **Reserved control tags:** the bot drives its workflow through control tags
  (`claude:*`/`resolver:*`, `repo:*`, `parent:*`, `review-by:*`, `dangerous`, `fix`,
  `delegate`). The **backend** restricts setting
  these to admins and the resolver bot, so an ordinary user can't hijack the
  automation (re-point a repo, force an "implement & open PR" phase, or strip
  the `dangerous` gate) via the UI or their own API key. For this to recognize
  the bot, set **`RESOLVER_BOT_USER_ID` in the backend's environment** to the
  bot's user id (the same id the resolver uses) — otherwise only admins can
  manage these tags and the bot's `set_state` transitions will be rejected.
  (Alternatively, promote the bot to `admin` and leave `RESOLVER_BOT_USER_ID`
  unset on the backend; the env-var approach keeps the bot least-privilege.)
