# 🐟 Stingray Tickets

A lightweight, self-hosted ticketing system for a homelab. Two use cases:

1. **Code review tickets** — created programmatically by Claude Code after finishing a task,
   attaching code snapshots (file paths, line ranges, content) and review notes.
2. **General task tickets** — created by family members for household task assignment.

FastAPI + SQLite backend, React/Vite frontend, deployed via Docker Compose behind Traefik.

## Stack

- **Backend:** FastAPI, SQLAlchemy, SQLite. Session auth (signed cookies) for browsers and
  `X-API-Key` header auth for programmatic clients.
- **Frontend:** React + Vite (plain JS), plain CSS modules, highlight.js for code rendering.
- **Deploy:** Docker Compose; frontend served by nginx (proxies `/api` to the backend);
  named volume for the SQLite database.

## Quick start (Docker)

```bash
cp .env.example .env        # then edit ADMIN_* and SESSION_SECRET
docker compose up --build
```

- Frontend: http://localhost:3000
- Log in with the `ADMIN_USERNAME` / `ADMIN_PASSWORD` you set in `.env`.

The initial admin is created automatically on first run (only when the database is empty).
Its generated API key is printed in the backend logs and is also visible on the **Profile**
page.

To put it behind Traefik, see [`traefik-labels.md`](./traefik-labels.md).

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

## API

Full REST documentation with curl examples and response shapes is in
[`api_guide.md`](./api_guide.md) — this is what Claude Code instances read to learn how to
create tickets.

## Roles

- **admin** — full access, user management, can modify/delete any ticket.
- **member** — create tickets, be assigned tickets, comment, and modify tickets they created
  or are assigned to.

## Project layout

```
ticketing/
  backend/      FastAPI app, models, auth, routers
  frontend/     React/Vite SPA
  docker-compose.yml
  traefik-labels.md
  api_guide.md
  .env.example
```

## Out of scope (for now)

Email notifications, file attachments beyond code blocks, OAuth/SSO, multi-workspace.
