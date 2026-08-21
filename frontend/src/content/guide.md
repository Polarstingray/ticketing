# Stingray Tickets — Guide

Stingray is a self-hosted ticketing app with an **optional** AI resolver that can plan,
implement, and review code-change tickets against your own repositories. This page is a
quick operator's guide; the full docs live in the repository's `README.md` and
`resolver/README.md`.

## What's in the box

- **Ticketing app** (everyone runs this): tickets with types, priorities, statuses,
  assignees, comments, code blocks, and an activity log. Session-cookie login plus
  per-user API keys.
- **Resolver** (optional, advanced add-on): a headless agent that picks up tickets
  assigned to its bot user, plans the change, opens a pull request, and reviews code.
  It needs an agent CLI (Claude Code or opencode) and your own provider API keys.
- **`stingray` CLI** (optional): files review tickets straight from a git checkout, so
  you never paste code by hand, and scaffolds new projects with a ready-made backlog.
- **Desktop app** (optional): a small Tauri client that wraps this web app and remembers
  which server you point it at.

## Running the app

The app ships as Docker Compose services (a FastAPI backend + a React frontend).

1. Copy `.env.example` to `.env` and set at least `SESSION_SECRET` and `ADMIN_PASSWORD`.
2. Run the installer, or bring the stack up directly:

   ```bash
   ./install.sh           # guided first-run setup
   # or
   docker compose up -d --build
   ```

3. Open the app, log in as the admin you configured, and create users from
   **Users** (admin only).

To run from prebuilt images instead of a source checkout, use
`docker compose -f docker-compose.images.yml up -d`.

## Working with tickets

- **New ticket:** click **New**, choose a type (`task` or `code_review`), set a priority,
  and write a description. `code_review` tickets can attach code blocks (file + line range).
- **Assign** a ticket to a user to make it theirs; assignment drives notifications.
- **Descriptions and comments are markdown** — headings, lists, tables, task lists
  (`- [ ]`), and fenced code blocks with syntax highlighting all render. Raw HTML is
  escaped rather than rendered.
- **Comments** thread the discussion, and their author (or an admin) can edit or delete
  one afterwards. The resolver also talks back through comments (posting plans, review
  findings, and PR links).
- **Due dates** are optional; a ticket past its date shows an **Overdue** badge in the
  list until it is closed.
- **Control tags** like `repo:<name>`, `dangerous`, `fix`, and `delegate` steer the
  resolver. They are reserved: only an admin or a resolver bot can set them, so a regular
  user can't hijack the automation.

## Finding tickets

The ticket list has a **filter panel**: status, type, priority, assignee, free-text
search, and multi-select tags (the tag picker offers only tags on tickets currently in
scope). Active filters show as chips you can clear one at a time, or all at once.

Any filter combination can be kept as a **saved view**. A view stores the list's query
string, so saving a view and sharing a filtered link are the same thing — and the same
string is what the resolver's digest uses to define its scope. Views are personal: nobody
else sees yours, admins included.

Archived tickets are hidden by default; a ticket can only be archived once it is closed.

## Notifications

Being assigned a ticket or getting a comment on one raises a notification, shown in
**Notifications**. **Settings** controls which of those reach you and how — in-app, by
email, or both — per event type. Email only goes out if the server has SMTP configured;
in-app notifications always work.

## The `stingray` CLI

Filing a code review by hand means pasting code into a description. The CLI turns a git
range into a ticket instead, with the changed hunks attached as code blocks:

```bash
pipx install ./cli
stingray auth login --url http://localhost:3000/api --bot-user-id 2

stingray review                    # last commit + working tree
stingray review HEAD~3..HEAD       # an explicit range
stingray review --describe         # let a local agent write the title/description
stingray review --assign-bot       # file it straight at the resolver

stingray file --type task --title "Flaky retry test" --priority low
stingray scaffold python-cli ./newproj --intent "a log parser"
```

Two things worth knowing before the first run:

- **The URL is the API base, so it usually ends in `/api`.** This deployment serves the
  app at the root and proxies `/api` to the backend, so pointing at the root returns the
  web page, not the API — you'll see _"returned text/html, expected JSON"_.
- **The key needs the `cli` scope** to tag tickets `repo:<name>`, which is what lets a
  resolver check the code out. Only an admin can mint a scoped key, from
  **Profile → API keys**. Without it the CLI still files tickets, they just can't be
  auto-fixed.

Credentials are stored at `~/.config/stingray/config.toml`, mode 0600.

## Guided projects

A guided project is a repository shaped like a class assignment: every non-trivial
function is left as a stub, each stub has its own ticket, and an `ASSIGNMENT.md` handout
lays out the brief, ordered milestones and a rubric. It is the inverse of the resolver's
usual job — the point is to _not_ implement the thing, so someone can.

A stub is two lines, machine-scannable and fatal if you forget it:

```python
def charge_card(token: str, cents: int) -> str:
    # STINGRAY-STUB: implement against the payment provider.
    # ACCEPTANCE: idempotent per token; raises on a declined card.
    raise NotImplementedError("STINGRAY-STUB")
```

There are two ways in, depending on whether the code exists yet.

**An empty directory** — the CLI renders a template:

```bash
stingray scaffold fastapi-spa ./hw3 --guided \
    --intent "a library loan tracker" --course-level intro --milestones 5
```

`--course-level intro|intermediate|advanced` controls how much of the design the handout
gives away, and `--milestones N` how many groups the stubs are gathered into.

**A repo that already has code** — file a ticket with `/scaffold <what to build>` in its
description and let the resolver do it. It stubs the feature in next to what is already
there, opens a pull request of the skeleton, and files the same backlog. Because it is an
ordinary `task` ticket, it plans first: you see the proposed skeleton and `/approve` it
before any code exists.

Either way you end up with an **epic** ticket and one **exercise ticket per stub**, linked
by an `epic:<id>` tag. That tag is deliberately not the resolver's reserved `parent:<id>`,
which would make each child self-driving — exactly wrong for a backlog meant to be worked
through by hand. Nothing in the backlog gets implemented for you.

**The handout never enters a commit.** `ASSIGNMENT.md` is gitignored on purpose: it is
coursework, not code, so a learner pushing their work doesn't publish the brief, and an
instructor can hand out a different one against the same skeleton. Since a gitignored file
is easy to lose, the whole handout is mirrored onto the epic ticket — on the resolver path,
posted as a comment. That copy is the one that survives.

## The optional resolver

The resolver is for teams that want tickets resolved by an AI agent. In short:

1. Provision a resolver bot. The easiest path is the CLI:

   ```bash
   python resolver/cli.py bot create claude-bot --desc "heavy refactors"
   ```

   This creates the bot user, mints its API key, and writes a ready-to-run
   `resolver/.env.<name>` — no id syncing.

2. Point it at your repositories (`PROJECTS_ROOT`) and choose an agent (`RESOLVER_AGENT`).
3. Run a sweep (or schedule one with cron/systemd):

   ```bash
   python resolver/cli.py run --env .env.claude-bot --dry-run
   ```

### How a ticket flows through the resolver

1. **Plan** — the resolver reads the ticket read-only and posts a proposed plan.
2. **Approve** — reply `/approve` (and re-assign the ticket to the bot) to implement, or
   `/revise <notes>` to adjust. With a plan-critique model configured, a review AI vets the
   plan first.
3. **Implement** — the resolver makes the change on a branch and opens a pull request.
4. **Review** — `code_review` tickets get read-only findings posted back, and the ticket
   is tagged `resolver:awaiting-fix`. To have those findings applied, don't file a new
   ticket: press **Apply fixes** on the ticket page (or comment `/fix` and re-assign it to
   the bot). `/review` asks for another read-only pass. Tagging a ticket `fix` up front
   makes the first review apply its own findings.

### Standard commands

Some jobs recur across every project. Putting a slash-command line in a ticket's
description makes the resolver run a premade prompt for it:

```
/security-audit

Focus on the new auth router.
```

The ticket's own title and description stay as supporting context. Bundled commands are
`/security-audit`, `/dependency-audit`, `/test-coverage`, and `/scaffold` (described
under **Guided projects** above); new ones are Markdown files dropped into
`resolver/commands/`, picked up on the next sweep.

### Managing resolvers from the UI

**Resolvers** in the nav (admins only) lists every resolver that has checked in — its
label, agent, model, and when it was last seen — and lets you edit the non-secret
tunables (models, verification command, escalation, delegation) without touching a
`.env` file. Secrets are
never editable here and never sent to the browser; they stay in each resolver's `.env`.

### Daily digest

The resolver only ever works what's assigned to it, so nothing looks at the backlog as a
whole. The digest does: on its own schedule it surveys a slice of the tracker and files a
single report ticket — a short summary over a checklist of everything it covered, grouped
into sections like overdue, high priority, awaiting your approval, and stale.

```bash
python resolver/cli.py digest --name daily --dry-run   # render it, file nothing
python resolver/cli.py digest --name daily             # file the report ticket
```

Each report is defined by a `[[digest]]` block in `resolver/digests.toml` holding a ticket
query — the same query string a saved view stores, so you can build a filter in this UI,
save it, and paste it in. Nothing auto-runs off a digest; it is a report for a human.

It needs `DIGEST_ADMIN_KEY`, an **admin** key: the API shows non-admins only tickets they
created or are assigned to, so a resolver's own key would quietly digest the bot's queue
instead of the backlog.

### Delegation (fan-out)

A `delegate`-tagged ticket lets a **lead** resolver decompose the work and hand sub-tasks to
other resolver bots. Each sub-task is **self-driving**: its assignee plans it and a review AI
**auto-approves** the plan before implementing (falling back to an unreviewed `dangerous`
implement only when no review AI is configured). Sub-tasks are one level deep and each opens
its own PR handed back to the original requester.

### Cost & token usage

Every resolver phase records its model, token usage, and cost. A ticket's **Agent runs**
section shows them, and a delegating ticket shows a **delegation total** across all its
sub-tasks. From the CLI, `python resolver/cli.py stats --ticket <id>` prints the same rollup.
