# Architecture

Stingray is two cleanly separated pieces that talk only over the REST API:

1. **The ticketing app** — a FastAPI backend + a React SPA. This is the whole
   product for most users.
2. **The optional AI resolver** — a headless agent that runs on a dev station,
   pulls bot-assigned tickets over the same public API, and opens pull requests.
   It has **no privileged backdoor**: it authenticates with an ordinary API key
   and is constrained by the same authorization rules as any other client.

## System topology

```mermaid
flowchart LR
    subgraph browser["Browser (SPA)"]
        UI["React + Vite<br/>session cookie"]
    end

    subgraph server["Server (Docker Compose)"]
        NGINX["nginx<br/>serves SPA, proxies /api"]
        API["FastAPI backend<br/>auth · RBAC · rate limit"]
        DB[("SQLite<br/>named volume")]
        NGINX -->|/api| API
        API --> DB
    end

    subgraph devstation["Dev station (optional)"]
        RES["Resolver<br/>headless agent loop"]
        WT["git worktree<br/>(isolated checkout)"]
        CLI["coding-agent CLI<br/>Claude Code / opencode"]
        RES --> WT
        RES --> CLI
    end

    UI -->|HTTPS| NGINX
    RES -->|"X-API-Key REST"| API
    RES -->|"gh pr create"| GH["GitHub"]
```

Two auth paths into the same API:

- **Browsers** use signed-cookie sessions (`session_version` embedded in the
  token lets a password/role change invalidate every outstanding cookie).
- **Programmatic clients** (the resolver, `curl`, CI) send an `X-API-Key`
  header. Keys are hashed at rest, named, individually revocable, and can expire.

## Backend module map

| Module | Responsibility |
|--------|----------------|
| `main.py` | App assembly, CORS, rate-limit handler, lifespan (migrate + seed) |
| `models.py` | SQLAlchemy ORM: users, API keys, tickets, comments, activity, notifications, agent runs |
| `auth.py` | Password hashing, session tokens, `X-API-Key` verification |
| `routers/` | Endpoint groups: `tickets`, `comments`, `users`, `auth`, `notifications`, `preferences` |
| `migrations.py` | Idempotent, additive column migrations applied on startup (no Alembic) |
| `startup.py` | Fail-fast secret checks — refuses to boot on a default `SESSION_SECRET` in production |
| `activity.py` / `inbox.py` | Audit-trail writes and the notification gate (`should_notify`) |
| `ratelimit.py` / `login_throttle.py` | Per-IP limits; pluggable Redis storage for multi-worker deploys |
| `seed.py` | First-admin bootstrap and optional resolver-bot provisioning |
| `chat/` | The optional in-app AI assistant: provider, context pack, prompt |

## Ticket lifecycle with the resolver

```mermaid
sequenceDiagram
    actor User
    participant API as Stingray API
    participant Res as Resolver
    participant Agent as Coding agent
    participant GH as GitHub

    User->>API: File ticket (assigned to claude-bot)
    Res->>API: Sweep — find bot-assigned tickets
    Res->>Agent: PLAN (read-only)
    Agent-->>Res: Proposed plan
    Res->>API: Post plan, reassign to user
    User->>API: /approve (reassign to bot)
    Res->>Agent: IMPLEMENT (in git worktree)
    Agent-->>Res: Diff
    Res->>Res: Verify (run tests in worktree)
    Res->>GH: Open PR
    Res->>API: Post PR link + agent-run cost, reassign to user
    User->>GH: Review & merge
```

Every phase the resolver runs is recorded back on the ticket as an **agent run**
(model, token usage, and USD cost), so the otherwise-invisible automation shows
up as an auditable, costed timeline in the UI. See
[`resolver-design.md`](./resolver-design.md) for the internals.

## Deployment

- **Dev:** Vite dev server (`:5173`) proxies `/api` to a locally-run backend
  (`:8000`).
- **Prod:** `docker compose up` builds two images — nginx-served SPA and the
  FastAPI backend — with the SQLite database on a named volume. Tagged releases
  also publish images to GHCR (`docker-compose.images.yml`) so you can run
  without building from source.
