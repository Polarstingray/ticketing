# 🐟 Stingray Tickets

[![Live demo](https://img.shields.io/badge/demo-live-2ea44f)](https://stingray-tickets-demo.fly.dev)
[![CI](https://github.com/Polarstingray/ticketing/actions/workflows/ci.yml/badge.svg)](https://github.com/Polarstingray/ticketing/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Release](https://img.shields.io/github/v/release/Polarstingray/ticketing?sort=semver)](https://github.com/Polarstingray/ticketing/releases)

A lightweight, **self-hosted ticketing system** you can stand up in one command — plus an
**optional AI resolver** that can pick up tickets, write the code, and open pull requests.

- **Core ticketing app (everyone):** issues/tasks and code-review tickets with status,
  priority, assignees, tags, due dates, comments, an activity trail, in-app + email
  notifications, and multi-key API access. Browser auth via signed-cookie sessions;
  programmatic auth via `X-API-Key`.
- **AI resolver (optional, advanced):** a headless agent in [`resolver/`](./resolver) that
  drives a coding-agent CLI (Claude Code or opencode) against your repos to plan, implement,
  review, and PR bot-assigned tickets. It needs an agent CLI and your own provider API keys,
  so it's strictly opt-in — the core app runs fine without it.

A common topology: run Stingray on a server (behind a reverse proxy/HTTPS) and run the
optional resolver on a dev station that pulls bot-assigned tickets and opens PRs.

## Live demo

**[stingray-tickets-demo.fly.dev](https://stingray-tickets-demo.fly.dev)** — sign in as
`admin` / `demopass123`. The API is browsable too, via live Swagger docs at
**[/api/docs](https://stingray-tickets-demo.fly.dev/api/docs)**.

Start with **“Review: batch the activity-feed queries”** — a code-review ticket the AI
resolver worked, showing what each phase of the run cost. Then open **“Harden the
resolver's git-worktree isolation”** to see cost roll up across the sub-tasks it
delegated.

> The agent-run data on the demo is **illustrative**: the resolver itself isn't deployed
> there, since it needs provider API keys and push access to a real repo. Everything else
> is the real app. The database is wiped and re-seeded on every restart, so poke at it —
> you can't break anything that won't fix itself.

## Walkthrough

![Walkthrough: the backlog, a resolver-worked ticket's cost timeline, the delegation rollup, and a create → comment → resolve loop](docs/video/walkthrough.gif)

No narration: sign in → the backlog → **filtering it down by tag** and re-sorting by
priority → a code-review ticket **the AI resolver worked**, showing what each phase cost →
the **delegation rollup** totalling the spend of sub-tasks it fanned out → a human
create → comment → resolve loop.
([Higher-quality MP4](docs/video/walkthrough.mp4) · recorded from the real app by
[`frontend/scripts/record-walkthrough.mjs`](./frontend/scripts/record-walkthrough.mjs).)

## Screenshots

| Ticket list | Ticket detail |
|---|---|
| [![Ticket list](docs/img/tickets.png)](docs/img/tickets.png) | [![Ticket detail](docs/img/ticket-detail.png)](docs/img/ticket-detail.png) |

| Filtering by tag | Resolver cost timeline |
|---|---|
| [![Filter panel with two tags selected](docs/img/filtering.png)](docs/img/filtering.png) | [![Agent-run cost timeline](docs/img/resolver-cost.png)](docs/img/resolver-cost.png) |

A filterable backlog with color-coded priority/status badges, tags and assignees.
The filter rail narrows it by **any number of tags at once** (all-of by default, with
an any-of toggle) alongside type, status, priority, assignee and free-text search —
and the whole filter state lives in the URL, so a filtered view is bookmarkable and
shareable, and **saved views** name the ones you keep coming back to. Tags that drive
the resolver's automation (`repo:`, `claude:`, `delegate`) are grouped separately from
the ones people file tickets under, so they don't drown out the latter.

Each ticket page carries highlighted code snapshots, threaded comments, an activity
trail, and — when the AI resolver has worked it — a per-phase, costed timeline of
agent runs (plan → implement → review) with token usage and a rolled-up spend that
follows the resolver's delegated sub-tasks.

> Screenshots are captured from the real UI by
> [`frontend/scripts/capture-screenshots.mjs`](./frontend/scripts/capture-screenshots.mjs),
> not hand-drawn mockups. It drives the demo container so the shots show the curated
> demo dataset rather than whatever a test run happened to leave behind.

## How it works

- **[docs/architecture.md](docs/architecture.md)** — system topology, module map, and
  the ticket lifecycle (with diagrams).
- **[docs/resolver-design.md](docs/resolver-design.md)** — a design write-up of the
  optional AI resolver: the plan→implement→verify→PR loop, worktree isolation,
  provider-agnostic runners, cost accounting, and the eval harness.

## Stack

- **Backend:** FastAPI, SQLAlchemy, SQLite. Session auth (signed cookies) for browsers and
  `X-API-Key` header auth for programmatic clients.
- **Frontend:** React + Vite (plain JS), plain CSS modules, highlight.js for code rendering.
- **Deploy:** Docker Compose; frontend served by nginx (proxies `/api` to the backend);
  named volume for the SQLite database.

## Quick start (Docker)

One command — generates a `SESSION_SECRET`, prompts for an admin password, brings
everything up, and (optionally) provisions the resolver bot and writes `resolver/.env`:

```bash
./install.sh
```

Or do it by hand:

```bash
cp .env.example .env        # then edit ADMIN_* and SESSION_SECRET
docker compose up --build
```

- Frontend: http://localhost:3000
- Log in with the `ADMIN_USERNAME` / `ADMIN_PASSWORD` you set in `.env`.

`make help` lists common tasks (`make up`, `make down`, `make test`, `make lint`).

The initial admin is created automatically on first run (only when the database is empty),
along with a first API key named `default` whose plaintext is printed **once** in the backend
logs — copy it then. Manage keys afterwards on the **Profile** page.

### Run from prebuilt images (no source build)

Tagged releases publish images to GHCR, so you can run without building from source — just
`docker-compose.images.yml` and a `.env`:

```bash
curl -O https://raw.githubusercontent.com/Polarstingray/ticketing/main/docker-compose.images.yml
curl -O https://raw.githubusercontent.com/Polarstingray/ticketing/main/.env.example
cp .env.example .env        # edit ADMIN_* and SESSION_SECRET
docker compose -f docker-compose.images.yml up -d
```

Pin a version with `STINGRAY_TAG` (defaults to `latest`), e.g. `STINGRAY_TAG=v1.0.0`.

To put it behind Traefik, see [`traefik-labels.md`](./traefik-labels.md).

## Deploying to a server (production)

A common topology: **Stingray runs on a server** (behind a reverse proxy with HTTPS, e.g.
Traefik), and the optional **resolver(s) run on dev stations** (see [`resolver/`](./resolver))
that pull bot-assigned tickets and open PRs. To harden a real deployment, set these in `.env`:

| Var | Production value |
|-----|------------------|
| `APP_ENV` | `production` — the backend then **refuses to start** with a default/empty `SESSION_SECRET` and warns on a weak `ADMIN_PASSWORD` |
| `SESSION_SECRET` | a long random string: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ADMIN_PASSWORD` | a strong, unique password (used only to seed the first admin) |
| `COOKIE_SECURE` | `true` when served over HTTPS (also implies production) |
| `CORS_ORIGINS` | your real origin(s), e.g. `https://tickets.example.com` |

```bash
cp .env.example .env        # set APP_ENV=production, a real SESSION_SECRET, etc.
docker compose up --build -d
```

### Public demo instance

[`deploy/demo/`](./deploy/demo) hosts a throwaway instance anyone can click around in.
It differs from a real deployment in three deliberate ways:

- **One container.** Hosts like Fly.io deploy a single image per app, so
  [`deploy/demo/Dockerfile`](./deploy/demo/Dockerfile) collapses the two compose services
  into one: nginx serves the built SPA and proxies `/api` to uvicorn on loopback.
- **Ephemeral, self-resetting data.** No volume is mounted, and the entrypoint runs
  `seed_demo --force` on boot — so every restart (including a scale-to-zero cold start)
  repaints the same illustrative dataset. That *is* the reset mechanism for a public
  instance, and the published login is throwaway by design.
- **No resolver.** The AI resolver needs provider API keys and push access to a real repo,
  so it isn't deployed; the demo ships illustrative agent-run data instead.

```bash
# Build/run it locally exactly as the host will (context is the repo root):
docker build -f deploy/demo/Dockerfile -t stingray-demo .
docker run --rm -p 3000:3000 stingray-demo        # http://localhost:3000 — admin / demopass123

# Or ship it to Fly.io. `app`/`PUBLIC_BASE_URL` in fly.toml must be renamed first:
# the app name has to be globally unique across Fly.
fly apps create stingray-tickets-demo

# Required. Session cookies are signed with this; unset, each machine would sign
# with its own key and the app would appear to log users out at random.
fly secrets set SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

fly deploy --config deploy/demo/fly.toml --dockerfile deploy/demo/Dockerfile .

# Required. The demo's SQLite DB is ephemeral and machine-local, so a second
# machine would serve a second, divergent dataset.
fly scale count 1
```

The walkthrough and the screenshots at the top of this README both come from that same
container, so they can be regenerated whenever the UI changes:

```bash
docker run -d --name stingray-demo-rec -p 3200:3000 stingray-demo

cd frontend
node scripts/capture-screenshots.mjs    # -> docs/img/*.png
node scripts/record-walkthrough.mjs     # -> docs/video/walkthrough.webm (gitignored)
scripts/encode-walkthrough.sh           # -> docs/video/walkthrough.{mp4,gif}
```

The recording's wall-clock runtime *is* the video length — the pauses are what make it
watchable — so expect it to take about as long as the clip.

### Scaling

The default deployment is a **single backend worker**, which is plenty for a team and keeps
things simple (SQLite + in-process rate limiting). If you scale to multiple workers or
replicas, the per-IP rate-limit/throttle counters must be shared, or each process counts
independently. Point them at Redis with one env var — no code change:

```bash
RATELIMIT_STORAGE_URI=redis://redis:6379
```

For heavy concurrent write load you'd also want to move off SQLite; that's a larger change
and not currently provided.

### Backups

The database is a single SQLite file on the `stingray-data` volume. Take consistent
online snapshots with [`backend/backup_db.py`](./backend/backup_db.py) (uses SQLite's
backup API — safe while the app is running):

```bash
# one-off, from the host:
docker compose exec backend python backup_db.py --keep 14
# -> writes /data/backups/stingray-<timestamp>.db inside the volume
```

Schedule it with cron/systemd on the host, or enable the optional `backup` sidecar in
`docker-compose.yml` (a commented-out service that snapshots daily and keeps 14).

### Schema migrations

There's no Alembic. Column additions are small idempotent steps in
[`backend/migrations.py`](./backend/migrations.py), applied automatically on startup after
`create_all`. To add one, append a function to `MIGRATIONS` following the pattern there.

## Features

- **Tickets** — `code_review` (with highlighted code snapshots) and `task` types; status,
  priority, assignee, tags, due dates; comments.
- **Activity trail** — every ticket records who created it, assignment, status/priority
  changes, and comments.
- **Multiple named API keys per user** — hashed at rest, optional expiry, revocable, with a
  `last used` timestamp. Rotate with zero downtime (create new → swap → revoke old).
- **Email notifications** (optional) — the assignee is emailed when a ticket is assigned, and
  admins are emailed when a ticket is created. Configure SMTP in `.env` (see below); leave
  `SMTP_HOST` blank to disable.

### Email configuration

Set these in `.env` to enable notifications (all optional; no email is sent if `SMTP_HOST` is
empty):

| Var | Purpose |
|-----|---------|
| `SMTP_HOST` / `SMTP_PORT` | SMTP relay (port default `587`) |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | auth, if your relay requires it |
| `SMTP_FROM` | From address |
| `SMTP_STARTTLS` / `SMTP_SSL` | transport security (`STARTTLS` default `true`) |
| `PUBLIC_BASE_URL` | base URL used to build clickable ticket links in emails |

## Local development

**Backend:**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ADMIN_USERNAME=admin ADMIN_PASSWORD=adminpass ADMIN_EMAIL=admin@example.com \
  SESSION_SECRET=dev-secret uvicorn main:app --reload --port 8000
```

**Frontend:**

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173, proxies /api -> localhost:8000
```

### Auto-deploy on this machine

`make up` rebuilds and restarts the stack at <http://localhost:3000>. To have that
happen by itself whenever a feature lands:

```bash
make hooks-install     # arm it
make hooks-uninstall   # disarm it
```

This installs `post-commit` and `post-merge` git hooks. On a commit or merge that
(a) is on `main` and (b) touches `backend/`, `frontend/` or `docker-compose.yml`,
it runs the backend and frontend test suites and — only if both are green —
rebuilds the images and restarts the containers. A red suite is logged and the
running build is left alone, so the box keeps serving the last good version
instead of going down with a broken one.

The hooks detach immediately, so `git commit` returns at once and the deploy
continues in the background. Budget roughly 5–8 minutes end to end — the bulk of
it is the two test suites, not the Docker build:

```bash
make deploy-log                      # watch it
make deploy                          # deploy now, any branch, same test gate
touch deploy/.autodeploy-disabled    # pause (delete the file to resume)
```

Two things worth knowing. The deploy builds from the **working tree**, not the
commit, so a dirty tree deploys uncommitted code — the log says so when that
happens. And `git commit --no-verify` does *not* skip it (that flag only covers
`pre-commit`/`commit-msg`), which is what the disable file above is for.

Editing `deploy/autodeploy.sh` while a deploy is running is safe: the script's body
is wrapped in a function called on its last line, so bash parses the whole file
before any work begins rather than reading it incrementally underneath itself.

The hooks are tracked in [`deploy/hooks/`](./deploy/hooks) and the logic lives in
[`deploy/autodeploy.sh`](./deploy/autodeploy.sh); `.git/hooks` only gets a shim
that execs them, so changing the behavior is an ordinary reviewable commit.
Tune the branch with `DEPLOY_BRANCH=` (empty means any branch).

**Demo data** (for a screenshot/walkthrough or a hosted demo — a lived-in board
with a resolved code-review ticket, its per-phase agent-run cost timeline, a
delegated parent→child fan-out, a *failed* implement run carrying the redacted
tail of its transcript, and a chat thread where the assistant explains that
failure and proposes a follow-up ticket):

```bash
cd backend
DATABASE_PATH=data/demo.db python -m seed_demo   # login: admin / demopass123
DATABASE_PATH=data/demo.db uvicorn main:app --port 8000
```

The seeded chat thread is stored, so reading it costs nothing — but the popup
hides itself entirely unless the assistant is configured (`ChatConfig.enabled`
needs all three of `CHAT_API_URL`/`CHAT_API_KEY`/`CHAT_API_MODEL`). For a demo
that only *browses* the thread, the values need not be valid:

```bash
CHAT_API_URL=https://demo.invalid/v1/chat/completions \
CHAT_API_KEY=demo-not-a-real-key CHAT_API_MODEL=claude-sonnet-5 \
DATABASE_PATH=data/demo.db uvicorn main:app --port 8000
```

Asking a *new* question of course needs a real provider.

## Testing

The CI matrix runs on every push (see [`.github/workflows/ci.yml`](./.github/workflows/ci.yml)):

- **Backend** — `pytest` (`backend/`), ~100 tests covering auth, RBAC, tickets,
  comments, notifications, migrations, and backups.
- **Resolver** — `pytest` (`resolver/`), 200+ tests of the agent loop with the CLI
  and API mocked (lifecycle, tag gating, path-allowlist, delegation, backoff).
- **CLI** — `pytest` (`cli/`), covering the credential store, git range resolution,
  diff→code-block mapping, the `--describe` fallback ladder, and scaffolding.
- **Frontend unit/component** — Vitest + Testing Library (`frontend/src`), covering
  permission gating, the dashboard's URL-backed filter state, tag selection, and
  pure helpers.
- **Frontend E2E** — Playwright (`frontend/e2e`) drives a real browser through the
  full stack: login → create → comment → resolve, multi-tag filtering with a
  shareable URL and saved views, and the resolver's costed agent-run timeline.
  These specs no longer write `docs/img/` — the README assets are captured
  separately (see [Public demo instance](#public-demo-instance)) so a test run
  can't overwrite them with whatever data it happened to create.

```bash
cd backend   && python -m pytest -q
cd resolver  && python -m pytest -q
cd cli       && python -m pytest -q
cd frontend  && npm test                       # unit/component
cd frontend  && npm run test:e2e:install        # one-time: fetch the browser
cd frontend  && npm run test:e2e                # end-to-end (boots backend + Vite itself)
```

## The `stingray` CLI

An installable command-line client lives in [`cli/`](./cli). It closes the loop
between writing code and getting it reviewed: it files tickets **from git**, so the
changed hunks become the ticket's code blocks automatically and you never paste code
by hand.

```bash
pipx install ./cli
stingray auth login --url http://localhost:3000/api --bot-user-id 2

stingray review                     # last commit + working tree
stingray review HEAD~3..HEAD        # an explicit range
stingray review --describe          # a local agent writes the title/description
stingray review --assign-bot -y     # file it straight at the resolver

stingray scaffold --list-templates            # python-cli, fastapi-spa
stingray scaffold fastapi-spa ./newapp --intent "a shared note-taking app"
```

`scaffold` renders a project template, optionally adapts it to your description with
a local agent, deliberately leaves the interesting functions stubbed, commits it, and
files one ticket per stub plus an epic that tracks them.

Every ticket it files carries a `repo:<name>` tag — that's what lets you assign it to
a resolver bot and press **Apply fixes**. Setting that tag needs an API key with the
**`cli` scope**, which an admin mints from Profile → API keys.

See [`cli/README.md`](./cli/README.md).

## API

Full REST documentation with curl examples and response shapes is in
[`api_guide.md`](./api_guide.md) — this is what Claude Code instances read to learn how to
create tickets.

## Roles

- **admin** — full access, user management, can modify/delete any ticket.
- **member** — create tickets, be assigned tickets, comment, and modify tickets they created
  or are assigned to.

## Desktop app

A native desktop client for Ubuntu Linux and macOS lives in [`desktop/`](./desktop),
built with [Tauri 2](https://tauri.app). It asks for your server's address on first
launch, then opens the normal web app in a native window and logs in exactly as the
browser does — no backend or frontend changes required. Cross-platform installers
(`.deb`/`.AppImage`/`.dmg`) are built and attached to GitHub Releases on tagged builds.

```bash
cd desktop && npm install && npm run tauri:dev    # develop
cd desktop && npm run tauri:build                 # build installers
```

See [`desktop/README.md`](./desktop/README.md) for prerequisites and details.

## Project layout

```
ticketing/
  backend/      FastAPI app, models, auth, routers, migrations, backup
  frontend/     React/Vite SPA
  desktop/      Tauri desktop client (Ubuntu + macOS) for a self-hosted server
  cli/          `stingray` CLI + the shared API client the resolver also uses
  resolver/     optional headless agent that resolves bot-assigned tickets
  install.sh    guided one-command setup
  Makefile      common tasks (make help)
  docker-compose.yml          build-from-source compose
  docker-compose.images.yml   run prebuilt GHCR images
  traefik-labels.md
  api_guide.md
  .env.example
```

## Out of scope

Single-organization by design: no multi-tenancy/workspaces, OAuth/SSO, or billing. File
attachments are limited to code blocks.

## Contributing

Issues and PRs welcome — see [`CONTRIBUTING.md`](./CONTRIBUTING.md) for dev setup and
conventions, and [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md). Release history is in
[`CHANGELOG.md`](./CHANGELOG.md).

## License

[MIT](./LICENSE) © Polarstingray. SPDX-License-Identifier: `MIT`.
