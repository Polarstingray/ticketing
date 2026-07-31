# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version of record is the `version` in `backend/main.py` and `frontend/package.json`
(kept in lockstep). Tag a release `vX.Y.Z` to publish images and a GitHub Release.

## [Unreleased]

### Added
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

### Fixed
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
