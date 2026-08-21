# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version of record is the `version` in `backend/main.py` and `frontend/package.json`
(kept in lockstep). Tag a release `vX.Y.Z` to publish images and a GitHub Release.

## [Unreleased]

### Added
- **Dashboard filter panel**: the ticket list's single row of dropdowns becomes a proper
  filtering surface — a sticky left rail on wide screens, a collapsible drawer below
  900px. It gathers search, type, status, priority, assignee and archived alongside the
  two new pieces below, shows how many filters are narrowing the list, and clears them in
  one click.
- **Multi-tag filtering**: `GET /tickets` now takes `tag` **repeatably**, combined by
  `tag_match=all` (default) or `any`. The picker splits free tags from workflow tags
  (`repo:*`, `claude:*`, `dangerous`, …) into a separate, collapsed group — on a busy
  instance the automation tags outnumber the ones people actually triage by several times
  over. `GET /tickets/tags` backs it with usage counts, honoring the same visibility rules
  as the list so it can't reveal a tag that exists only on someone else's ticket.
- **Sorting**: `sort=created|updated|priority|due|title` with `order=asc|desc`, exposed as
  a dropdown in the list header. `priority` sorts by rank rather than alphabetically, and
  tickets with no due date sort last in either direction.
- **Shareable filter URLs**: the dashboard's whole filter state now lives in the query
  string, so a filtered view is bookmarkable, survives the back button after opening a
  ticket, and can be pasted to a teammate. Defaults are omitted, so an unfiltered list
  keeps a clean `/tickets` address.
- **Saved views**: name a set of filters and come back to it. Because a view stores the
  raw query string, a saved view and a shared link are the same object. Scoped strictly to
  their owner — admins included — via `GET/POST/PATCH/DELETE /saved-views`.
- **Compact row density** on the ticket list, remembered per browser. It is a viewing
  preference, not part of the query, so it deliberately stays out of shared URLs.
- **`stingray` CLI** (`cli/`, pipx-installable): files code-review tickets straight from
  git, turning the changed hunks into the ticket's code blocks. `stingray review`
  defaults to the last commit plus working-tree changes; `stingray file` replaces the
  hand-written `curl`; `stingray auth` stores per-profile credentials in
  `~/.config/stingray/config.toml` at mode 0600.
- **`--describe`**: an optional pass that shells out to a local agent (`claude` or
  `opencode`) to write a ticket's title, description and priority from the commits and
  diff. It never blocks filing — a missing agent, timeout or unparseable output falls
  back to the commit-derived text.
- **`stingray scaffold`**: renders a project template, optionally adapts it to a
  one-line intent with a local agent, leaves the interesting functions marked
  `STINGRAY-STUB:`, commits, then files one ticket per stub plus a tracking epic. Stub
  tickets are grouped by a free `epic:<id>` tag, never the reserved `parent:<id>` (which
  would make each one self-driving). Two templates ship: `python-cli` and
  `fastapi-spa` (FastAPI + React, stubbed on both sides of the wire).
- **Guided projects**: two front doors onto one engine, for building repos shaped like a
  CS-class assignment. `stingray scaffold --guided` adds an `ASSIGNMENT.md` handout
  (learning goals, ordered milestones with a "Done when" each, a rubric, real run
  commands) and rewrites the stub tickets as exercises; `--course-level` and
  `--milestones` tune it. The handout is **gitignored on purpose** — it is coursework,
  not code — so everything down to the rubric is mirrored into the epic ticket, which
  becomes the copy that survives. `--no-ai` and any agent failure fall back to a handout
  generated from the scanned stubs.
- **`/scaffold` standard command**: the existing-codebase counterpart. A ticket whose
  description carries `/scaffold <what to build>` has the resolver stub the feature into
  a repo that already has code — plan first, so a human approves the skeleton before it
  exists — then open a PR of stubs only. `resolver/scaffold_followup.py` then lifts the
  handout out of the worktree before the commit (a gitignored file can never ride a PR)
  and posts it as a comment, and scans the touched files to file one exercise ticket per
  `STINGRAY-STUB`. Scanning, not log-scraping, is what makes a ten-plus-stub backlog
  exact. Children carry `epic:<id>` and never `parent:` — a self-driving exercise ticket
  would defeat the point. Re-runs rebuild the skeleton without refiling the backlog.
- **Scoped API keys**: `ApiKey.scopes`, with a `cli` scope that permits `repo:<name>`
  tags and no other reserved tag. Scopes are **admin-granted only** — any member can mint
  their own keys, so self-service scoping would be no boundary. Surfaced on Profile →
  API keys.
- **One-command install** (`install.sh`) and a `Makefile` of common tasks.
- **Automatic resolver-bot provisioning**: with `SEED_RESOLVER_BOT=true` the backend seeds a
  least-privilege bot user, mints its API key, and writes a bootstrap file the installer uses
  to fill in `resolver/.env`.
- **Resolver standard commands**: invoke a premade prompt (e.g. `/security-audit`) from a
  ticket body; composes with the `delegate` tag for audit-then-fan-out.
- **Published container images** to GHCR on tagged releases, plus
  `docker-compose.images.yml` to run them without a source build.
- Project governance docs: `LICENSE` (MIT), `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, this
  changelog, and GitHub issue/PR templates.
- **Markdown in comments and descriptions**: ticket comment bodies and descriptions now
  render as markdown — headings, lists, tables, task lists, links, inline code, and
  syntax-highlighted fenced code blocks with a copy button. The resolver already wrote
  markdown (bold state markers, severity-grouped findings, `diff` fences, the `/scaffold`
  handout with its rubric table); it now reads as intended instead of as literal text.
  The comment composer gained a Write/Preview toggle. No schema change — raw bodies are
  stored and served unchanged, so the resolver's marker parsing is unaffected. Raw HTML
  in a body stays escaped (no `rehype-raw`).

### Fixed
- Ruff linted `cli/build/`, a stale *copy* of the CLI sources left by packaging.
  Every finding there duplicated one in `cli/stingray_cli/`, inviting a fix in the
  throwaway copy that the real sources would never see. Build output is now
  excluded — it is gitignored, but `ruff.toml` sets `respect-gitignore = false`,
  so ruff walked it anyway.
- `make backend-test`, `resolver-test`, `cli-test` and `lint` invoked bare `python`
  and `ruff`, neither of which is on PATH on a stock Debian/Ubuntu box — so they
  failed as "command not found" rather than as a test result. Each target now
  resolves an interpreter at run time (the project's own `.venv`, then the
  backend's, then PATH) and reports a useful error when pytest is missing.
  Override with `make backend-test PY=/path/to/python`.
- The `tag` filter escaped no LIKE wildcards, so a tag containing `_` (legal in a tag)
  matched any character in that position — `a_b` also matched `axb`.
- **Spoofable client IP** (security): nginx forwarded `X-Forwarded-For` by *appending* to the
  caller's own value, and uvicorn (`--forwarded-allow-ips "*"`) trusts the leftmost entry, so
  any client could choose its `request.client.host` — dodging the per-IP login limit and the
  API-key throttle, and planting an unbounded number of throttle entries. Both nginx configs
  now overwrite the header with the address nginx resolved itself.
- **Unbounded auth-throttle memory**: the in-memory account-lockout and per-IP failure maps
  (`backend/login_throttle.py`) never evicted anything. They now sweep aged-out entries and
  enforce a hard cap, shedding harmless entries before live lockouts. Login credentials are
  also length-bounded so a single request can't plant a megabyte-sized key.
- **Permanent account lockout by a third party**: the failure counter now decays after a quiet
  window, so an attacker who knows a username can no longer ratchet that account to the 1-hour
  lockout cap and hold it there. Arming a lockout is logged.

### Changed
- Tag chips are now one shared component across the ticket list and detail pages. Reserved
  workflow tags get the dashed, striped treatment everywhere (previously only on the
  detail page), and derived badges like *Archived* and *Overdue* no longer look identical
  to real tags on a list row.
- The REST client and ticket-payload helpers moved to `cli/stingray_client/`, shared by
  the CLI and the resolver. `resolver/stingray.py` subclasses the client to re-add audit
  logging and `resolver/file_ticket.py` adapts to the library, so both keep their exact
  previous behavior and command-line surfaces.
- Ticket tag authorization is now per-tag (`control_tags.can_set_tag`) rather than
  all-or-nothing, and the reserved-tag error message is generated from the constants —
  the old fixed string had gone stale, naming four of the seven reserved forms.
- A successful API-key request now credits back one failed attempt for that IP instead of
  clearing the whole per-IP counter.
- The resolver bot is now recognized for control-tag permissions by a DB flag
  (`User.is_resolver_bot`) instead of a `RESOLVER_BOT_USER_ID` env id that had to be kept in
  sync between the backend and resolver. The legacy env id is still honored.
- README reworked for general self-hosting (core app vs. optional AI resolver).

## [1.0.0]

### Added
- Initial release: self-hosted ticketing with `task` and `code_review` ticket types; status,
  priority, assignee, tags, due dates, comments, and an activity trail.
- Session (signed-cookie) auth for browsers and `X-API-Key` auth for programmatic clients,
  with multiple named, revocable API keys per user.
- Admin/member roles with row-level access control; reserved control tags restricted to
  trusted identities.
- In-app and optional SMTP email notifications; per-user notification preferences.
- Optional headless resolver that plans, implements, reviews, and PRs bot-assigned tickets.
- Docker Compose deployment (nginx-served SPA proxying `/api`), SQLite with online backups,
  and CI running backend/resolver tests, lint, and the frontend build.

[Unreleased]: https://github.com/Polarstingray/ticketing/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Polarstingray/ticketing/releases/tag/v1.0.0
