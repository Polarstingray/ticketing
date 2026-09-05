# Resolver stations — handoff

*Written 2026-09-05. Read this before picking the work up; several of the
things below cost hours to learn and are not visible from the code.*

## What this is

A **station** is the set of resolvers running on one host. The work exists
because a resolver identity was four separate things that nothing forced into
agreement:

1. a bot **user** on a Stingray server (`is_resolver_bot`)
2. an **API key** for it, shown once at creation
3. an **`.env.<name>`** in a resolver checkout (`RESOLVER_ENV_FILE` selects it)
4. a **systemd unit pair** — `<prefix>@<name>.timer` and `<prefix>-listen@<name>.service`

`stingray station` makes them one named thing. The full design is in
`cli/README.md` → "`station`"; the resolver side is in `resolver/README.md` →
"Push wakeup".

## State of play

**Landed on `main`:** milestone 1 (`stingray station`), and the lease-release
fix (#148).

**NOT on `main` yet — see PR #152.** Milestones 3 and 4 are stranded. #150 and
#151 both report as *merged*, but they merged into their stacked bases rather
than into `main`:

- #150 merged `feat/station-heartbeat` → `cli/station`
- #151 merged `feat/station-enrollment` → `feat/station-heartbeat`
- #149 merged `cli/station` → `main`, but *before* #150 landed in it

Verify by content, never by PR state:

```bash
git show main:resolver/listen.py | grep -c 'class Heartbeat'          # 0 on main
git cat-file -e main:backend/routers/enrollments.py 2>/dev/null       # absent
```

**PR #152** brings the six stranded commits onto `main` and merges cleanly.
Land it first; nothing below makes sense on top of a `main` that lacks it.

## What each milestone did

| | Status | What |
|---|---|---|
| **M1** | on main | `stingray station init/adopt/ls/status/start/stop/restart/sweep/logs` |
| **M2** | **not started** | `doctor`, `config` with layer provenance. `new` was absorbed by M4 |
| **M3** | in #152 | listener heartbeats; workers report `station` + `heartbeat_seconds`; roster groups by host; freshness sized from the reported cadence |
| **M4** | in #152 | enrolment tokens — a host gets one bot's credentials without an admin key. Plus the API keys panel |
| **M5** | not started | TUI behind `pip install 'stingray-cli[tui]'` |

## What to do next

### 1. Land PR #152

Then three things follow immediately:

- **Restart the four local listeners** so they actually heartbeat:
  `systemctl --user restart 'stingray-resolver-listen@{claude,claude-lite,gemini,open}.service'`
- **Rebuild the local containers** (`docker compose up -d --build`) — M3 adds a
  migration and M4 adds endpoints and UI.
- **Re-check Stingray #191.** It may largely evaporate: it exists because a push
  wakeup that loses its claim is dropped, and the claims it was losing were
  almost all the leaked leases #148 fixed.

### 2. `doctor` (the highest-value remaining piece)

Every check in it is a failure that actually happened in the session that built
this, not a hypothetical:

- a unit installed but the listener never connected
- `STINGRAY_URL` doubling its `/api` prefix (this made push wakeup silently
  never work; the daemon looked healthy while every connection 404'd)
- a user unit driven without `--systemctl-user`
- `logs/` missing under a `StandardOutput=append:` unit — the unit fails outright
- one bot id claimed by two identities (bot 6 really is, on the home server)
- a dirty or detached checkout
- a bot with no local identity, or an identity whose bot no longer exists

### 3. Then, roughly in order

- **Stingray #192, #193** — decisions are already written into the tickets as
  comments; they are ready to hand to a resolver. Take #192 first: it is the one
  that stops a resolver deleting a human's uncommitted work.
- **Stingray #194** — small and crisp, no decision written yet.
- **Stingray #189's four MINORs** — the symlink one is real: `.env.claude` and
  `.env.claude-ubvm` are both symlinks in practice.
- **`config` with layer provenance** — `claude-lite` is the only identity with a
  server-side overlay, and nothing currently shows you that a value came from
  there rather than from `.env`.
- **M5 TUI** — last, behind an optional extra so the CLI stays stdlib+requests.

## This host, concretely

Nine identities, two checkouts, two servers. Bot ids repeat *across* servers, so
a bot id alone never identifies a resolver — only a (server, id) pair does.

| Checkout | Server | Identities |
|---|---|---|
| `~/projects/ticketing` | `http://localhost:3000/api` | `claude` (2), `gemini` (3), `open` (4), `claude-lite` (5), `station-test` (7) |
| `~/.ticketing` | `https://tickets.polarstingray.dev` | `claude-ubvm` (3), `claude-lite` (4), `claude-ubvm-heavy` (5), `mistral-bot` (6) |

Unit families are `stingray-resolver@` and `stingray-ubvm@` respectively — the
prefix is the only thing keeping the two `claude-lite` identities apart, and the
station inventory records it rather than guessing.

`tickets.polarstingray.dev` is **not** a cloud host: it resolves to `10.0.0.10`,
a machine on this LAN behind nginx. SSE passes through it unbuffered.

`station-test` (bot 7) was created by the first live run of `stingray station
enroll` as a demo. Its units are not installed. Delete the user and
`resolver/.env.station-test` if it is not wanted.

The CLI has two profiles: `default` → the home server, `local` → localhost.
Without a profile whose URL matches, `station init` **skips** an identity rather
than filing it under the wrong server — that is deliberate.

## Things that will cost you a day if you do not know them

**`LoadState=loaded` does not mean a templated unit is installed.** systemd
reports `loaded` for *every* conceivable instance of a live template, so
`stingray-ubvm@nonexistent.timer` looks as real as a running one. Installation is
an enable symlink (`UnitFileState`) or an active unit.

**The units run out of live git checkouts.** Switching a branch changes what
every resolver on the host executes. This nearly reintroduced a fixed bug under
four running listeners, and a resolver's worktree-escape cleanup once *deleted*
uncommitted edits made while a run was in flight (Stingray #192). Commit before
touching a watched checkout, and check
`systemctl --user is-active 'stingray-*@*.service'` first — `activating` means an
agent is working.

**`journalctl --user` shows nothing on this host.** The account is outside `adm`
and `systemd-journal`, and user-unit output goes to the system journal. That is
why the units append to `resolver/logs/<unit>-<identity>.log`. Parse those files.
`sudo usermod -aG systemd-journal penguin` plus a re-login is the real fix.

**CI lints before it tests.** `ruff check backend resolver cli` and, in
`frontend/`, `npm run lint`. A green pytest/vitest run says nothing about
whether CI will pass; both linters were missed twice for exactly this reason.

**The exported `STINGRAY_API_KEY` in the shell is revoked.** Prefix ticket-filing
commands with `env -u STINGRAY_URL -u STINGRAY_API_KEY` so the CLI profile is
used instead.

**`stingray` is pipx-installed.** After changing `cli/`, run
`pipx install --force ./cli` or the command on `PATH` stays stale.

**Two heartbeat reporters share one registry row.** The sweep knows
`effective_config`; the listener knows `station` and `heartbeat_seconds`. The
endpoint applies only the fields a caller actually sent — send a field you have
no value for and you blank what the other reporter wrote.

## Verifying it all still works

```bash
cd backend  && ../resolver/.venv/bin/python -m pytest -q    # ~590, takes 3-4 min
cd resolver && .venv/bin/python -m pytest -q                # ~470
cd cli      && ../resolver/.venv/bin/python -m pytest -q    # ~270
cd frontend && npx vitest run && npm run lint               # ~195, slow (~80s)
ruff check backend resolver cli                             # pinned 0.15.16
stingray station ls                                         # every identity on this host
```
