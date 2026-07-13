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
- **Comments** thread the discussion. The resolver also talks back through comments
  (posting plans, review findings, and PR links).
- **Control tags** like `repo:<name>`, `dangerous`, `fix`, and `delegate` steer the
  resolver. They are reserved: only an admin or a resolver bot can set them, so a regular
  user can't hijack the automation.

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
4. **Review** — `code_review` tickets get read-only findings posted back; add `fix` to also
   apply them.

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
