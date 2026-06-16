# Contributing to Stingray Tickets

Thanks for your interest in improving Stingray! This guide covers local setup, running the
test suites, and the conventions PRs are expected to follow.

## Project layout

- `backend/` — FastAPI + SQLAlchemy + SQLite API (Python 3.12).
- `frontend/` — React + Vite single-page app (Node 20, plain JS).
- `resolver/` — optional headless agent that resolves bot-assigned tickets.

## Local development

The fastest path is Docker:

```bash
./install.sh          # or: cp .env.example .env && docker compose up --build
```

To work on a component directly:

**Backend**
```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ADMIN_USERNAME=admin ADMIN_PASSWORD=adminpass SESSION_SECRET=dev-secret \
  uvicorn main:app --reload --port 8000
```

**Frontend**
```bash
cd frontend
npm install
npm run dev           # http://localhost:5173, proxies /api -> localhost:8000
```

**Resolver** (optional, advanced — needs an agent CLI + provider API keys)
```bash
cd resolver && ./setup.sh
```

## Running tests & lint

`make test` runs everything; or per component:

```bash
make backend-test     # cd backend && python -m pytest -q
make resolver-test    # cd resolver && python -m pytest -q
make frontend-test    # cd frontend && npm test
make lint             # ruff check backend resolver
```

CI (`.github/workflows/ci.yml`) runs the same suites on every push and PR. Please make sure
they pass locally before opening a PR.

## Conventions

- **Match the surrounding code.** Mirror the existing style, naming, and comment density in
  the file you're editing rather than introducing a new pattern.
- **Tests with behavior changes.** Add or update tests alongside any change to behavior; new
  endpoints and auth/permission changes especially need coverage.
- **Schema changes** use the lightweight idempotent migrations in `backend/migrations.py`
  (no Alembic) — append a step following the existing pattern.
- **Keep secrets out of the repo.** Never commit a real `.env`, API key, or password.
- **Commit messages**: a concise imperative subject line, with a body explaining the *why*
  when it isn't obvious.
- **Scope PRs narrowly** and describe what changed and how you verified it. Update
  `CHANGELOG.md` under "Unreleased" for user-facing changes.

## Reporting bugs / requesting features

Use the issue templates under `.github/ISSUE_TEMPLATE`. For security-sensitive reports,
please avoid filing a public issue with exploit details — see the note in the security
template.
