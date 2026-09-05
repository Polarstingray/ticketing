# `stingray` — the Stingray Tickets CLI

Files code-review tickets straight from git, optionally writes their prose with a
local AI agent, and scaffolds new projects with a ready-made backlog. Every
ticket it files carries a `repo:<name>` tag plus a `rev:<sha>`/`branch:<name>` pin,
which is what lets you assign it to a resolver bot and press **Apply fixes** — and
what makes the resolver review *the commit you filed from* rather than whatever
branch your checkout is on when the sweep runs.

## Install

```bash
pipx install ./cli          # from a checkout
stingray auth login --url http://localhost:3000/api --bot-user-id 2
```

`auth login` prompts for an API key (never echoed) and validates it before
storing. Credentials live in `~/.config/stingray/config.toml` at mode 0600.

> **The URL is the API base, so it usually ends in `/api`.** In the default
> deployment nginx serves the SPA at the root and proxies `/api` to the backend,
> so `http://localhost:3000` returns `index.html` — not the API. If you point at
> the root you'll get *"returned text/html, expected JSON"*. A backend you reach
> directly (e.g. `http://localhost:8000`) needs no suffix. Printed ticket links
> drop the `/api` again, since that prefix isn't part of a page address.

> **The key needs the `cli` scope.** `repo:`, `rev:` and `branch:` are reserved
> control tags; only an admin can mint a scoped key, from **Profile → API keys**.
> Without it the CLI can still file tickets, but they won't carry a repo tag or a
> commit pin, and the resolver won't be able to check the code out.

## What a review ticket pins

`stingray review` records **where** the code is, not just which repo:

| tag | what it does |
| --- | --- |
| `repo:<name>` | which checkout under the resolver's `PROJECTS_ROOT` |
| `rev:<full-sha>` | the commit reviewed — the resolver checks this out, detached |
| `branch:<name>` | where a fix stacks, and the base its PR targets |

Without the pin the resolver falls back to the repo's default branch, which is
wrong in the common case: you branch out, build a feature, file a review, then
switch branches — and the review reads code the ticket was never about while the
fix is cut from `main`, landing beside your work instead of on it.

Two things worth knowing:

- **Detached HEAD** files `rev:` with no `branch:`; the review is still exact, but a
  fix has no branch to stack on and falls back to the default.
- **Uncommitted changes** are captured in the ticket's code blocks, but they exist
  in no commit, so `rev:` can't reproduce them — the resolver reads the repo at the
  pinned commit. `stingray review` warns when this applies. Commit first if you want
  the surrounding-code reads to match exactly.

## Commands

```bash
stingray review                      # last commit + working tree
stingray review HEAD~3..HEAD         # an explicit range
stingray review my-branch            # merge-base(main, branch)..branch
stingray review --staged             # what you're about to commit
stingray review --describe           # let a local agent write the prose
stingray review --assign-bot -y      # file it straight at the resolver

stingray explore                     # a review ticket per feature in this repo
stingray explore --teach             # …written to teach a student the codebase
stingray explore --feature auth      # just one named feature

stingray file --type task --title "Flaky retry test" --priority low
stingray scaffold python-cli ./newproj --intent "a log parser"
stingray auth status

stingray station init                # adopt this host and everything on it
stingray station ls                  # every resolver here, and its state
stingray station status gemini       # units, last sweep, stream, checkout
stingray station logs gemini -f      # follow one resolver's sweep log
stingray station stop gemini         # timer and listener together
stingray station enroll st_7fQ2… --url URL --checkout DIR   # no admin key needed
```

### Credential precedence

Highest first: `--url` / `--api-key` flags, then `STINGRAY_URL` /
`STINGRAY_API_KEY` in the environment, then the selected profile.

This is deliberately the **opposite** of `resolver/config.py`, where the `.env`
file wins over the ambient environment. The resolver is a daemon whose identity
must not shift with whatever a shell exported; an interactive CLI should honor an
explicit override. Don't "fix" one to match the other.

## `station` — the resolvers running on this host

A resolver identity is four things that have to agree: a bot user on a Stingray
server, an API key for it, an `.env.<name>` in a resolver checkout, and a pair of
systemd units. Nothing forced them to agree before, and the ways they drift are
quiet — a bot id claimed twice, a unit pointing at the wrong checkout, a listener
that has never connected. `station` makes them one named thing.

```
$ stingray station ls
station 'ubvm.home.lab' — /home/penguin/.config/stingray/stations.toml
NAME                 BOT   PROFILE      STATE                CHECKOUT
claude               2     local        running              main @7a555b3
claude-lite          5     local        running              main @7a555b3
claude-lite@home     4     home         running              main @7a555b3
mistral-bot          6     home         running              main @7a555b3
```

**The inventory is local-first.** `~/.config/stingray/stations.toml` records
*intent* — which identities this host means to run — and nothing derived.
Unit state, checkout revision, last sweep and server settings are read fresh
every time, because a station has to be usable when the server is unreachable,
which is exactly when someone needs it.

**A station is per-host and spans servers.** One box commonly runs resolvers
against several Stingray instances; each identity names the profile whose URL it
matches. An identity whose URL matches no profile is *skipped*, never filed under
a fallback — putting a localhost resolver under another server's profile is the
mislabelling this command exists to prevent.

**Handle vs instance.** The table key is a station-unique *handle*; the systemd
instance name and `.env` suffix are separate, and differ only when they must. A
host running a `claude-lite` against two servers — two different bots — keys one
of them `claude-lite@home` while both keep the instance name their units already
use. Commands take either, and refuse a bare instance name that means two things.

**One bot, one resolver.** The server keeps a single registry row per bot
(`AgentInstance` is unique on `user_id`), so two identities sharing a bot would
overwrite each other's heartbeat and make both flicker between live and dead.
`init` reports the clash and keeps whichever one is actually enabled in systemd —
a stale `.env` beside a live one is the common case, and picking alphabetically
would adopt the dead one.

**`LoadState` is not installation.** systemd reports `loaded` for *every*
conceivable instance of a live template, so `stingray-ubvm@nonexistent.timer`
looks as real as a running one. An instance someone actually asked for has an
enable symlink or is active; that is what `status` reports.

### Enrolling a resolver without an admin key

Creating a bot and minting its key are admin operations — but the host that
*runs* the bot is also the host executing untrusted agent output, which makes it
the last place an admin credential should live. So an admin mints a one-shot
token in the web app (Resolvers → **Enrol a resolver**) and the workstation
redeems it:

```bash
stingray station enroll st_7fQ2… \
  --url https://tickets.example/api \
  --checkout ~/projects/ticketing \
  --start
```

That single call creates the bot, collects its API key, writes `.env.<name>`
from the checkout's `.env.example` at 0600, records the identity in the station
inventory and — with `--start` — installs the units and brings it up. The four
things that have to agree are created together, which is the whole reason the
command exists.

The token is **single-use and short-lived** (an hour by default), because an
unredeemed one is a standing capability to create a bot. Minting is gated on
`require_recent_admin`, which an API key cannot satisfy *at all* — it reads the
session cookie's age. That gate is the feature rather than a formality: if a
program could mint one, holding an admin key on the workstation would be no
worse.

`enroll` is the one command that works with no credentials, since a host
enrolling its first resolver has none. It needs `--url` for that reason. If no
configured profile matches that URL the identity is still written and usable —
only this host's bookkeeping is missing — and the command says exactly which two
commands close the gap rather than failing after the token is already spent.

`station` drives systemd rather than supervising anything itself. systemd already
serializes runs of a unit, merges a start into a queued job (which is what turns
a burst of assignments into one sweep), restarts a dead listener, survives logout
via linger and returns at boot. A tool that spawned its own children would lose
all of that and die with the terminal.

## How `review` builds code blocks

It parses the hunk headers of `git diff -U3 <range>`, takes the **post-image**
line ranges, merges hunks less than 10 lines apart, and reads the content.

Where that content comes from matters:

- working-tree changes → read off disk
- a committed range → `git show <rev>:<path>`

Reading disk for a historical range pairs that commit's line numbers with a
drifted worktree — wrong in a way a reviewer would never catch. Generated,
vendored and binary paths are skipped, and there are caps on block count, block
length and total lines (see `--max-blocks`, `--max-block-lines`,
`--max-total-lines`).

## `--describe`

Shells out to `claude` or `opencode` (whichever is on PATH; `--agent` picks one)
with the commits, diffstat and truncated diff, and asks for a JSON object with a
title, description, priority and tags. Tags that come back reserved are dropped
rather than sent.

**It never blocks filing.** No agent installed, a non-zero exit, a timeout, or
unparseable output all fall back to the commit-derived description with a warning.
Pass `--require-describe` to make those hard failures instead.

## `explore` — a reading guide for a codebase

`review` files a ticket about a **change**. `explore` files tickets about what is
already there: it lists the tracked files, hands them to a local agent (same
`claude`/`opencode` wrapper `--describe` uses), asks it to carve the repo into
significant features, and files one `code_review` ticket per feature. Each ticket
quotes up to three representative files and inherits the usual `repo:` / `rev:` /
`branch:` pin, so the resolver can pick any of them up and review it in place.

- **`--teach`** switches the prose from reviewer-facing to student-facing: the
  *why* behind the design, how the feature connects to the rest of the system,
  patterns worth generalizing, the non-obvious details, and a couple of questions
  the reader should be able to answer afterwards. This is the mode to use on a
  codebase you are trying to learn rather than audit.
- **`--feature NAME`** scopes the run to one feature and files a single ticket.
- **`--max-features N`** caps how many tickets a run can file (default 10).

Unlike `--describe`, there is **no deterministic fallback** here — there is no
git-derived answer to "what are this codebase's features" — so an agent that is
missing, times out, or returns unparseable output makes the command fail rather
than file something invented. Features whose files can't be read (a hallucinated
path, a deleted file) are dropped with a warning instead of becoming an empty
ticket. A feature that names *some* unreadable files is still filed — the readable
ones are worth a ticket — but the ones that got dropped are named on stderr, since
otherwise the description references code the ticket does not quote. Paths that are
absolute or traverse out of the repo are refused outright rather than read. `--dry-run`
prints the payloads and makes no network call, so it is safe to run before you have
credentials stored (including with `--assign-bot`, which warns rather than fails when
no bot id is configured).

Two things to know about **provenance**: the agent reads your *working tree*, but
tickets quote each file as of the last commit, so `explore` warns when the tree is
dirty — commit first if you want the prose and the quoted code to agree. And the
agent, model and timeout defaults come from the profile's existing
`[profile.<name>.describe]` stanza, shared with `review --describe`; override per-run
with `--agent`, `--timeout`.

The resolver has the same feature as a standard command: put `/codebase-review`
(optionally with the word `teach`) in a bot-assigned ticket's description and it
fans out one child ticket per feature. See `resolver/commands/codebase-review.md`.

## `scaffold` and the stub convention

> **`--intent`, not `--describe`.** These are different options that briefly
> shared a name. On `review`, `--describe` is a *boolean* asking an agent to
> write the ticket's prose (code in, text out). On `scaffold`, `--intent TEXT`
> is what you want built (text in, code out) — and unlike review's read-only
> pass, it lets the agent **edit files**. `--describe` still works on `scaffold`
> but warns.

A stub is two comment lines plus a raise:

```python
def read_source(source: str) -> str:
    """Read `source` from disk, or from stdin when it is '-'."""
    # STINGRAY-STUB: support both a file path and '-' for stdin.
    # ACCEPTANCE: raises FileNotFoundError with the path named when it is missing.
    raise NotImplementedError("STINGRAY-STUB")
```

`STINGRAY-STUB:` is what triggers a ticket (matched by a comment-syntax agnostic
regex, so any language works); `ACCEPTANCE:` becomes the ticket's acceptance
criteria.

Ordering is load-bearing: the scaffold is **committed before any ticket is
filed**, because code blocks carry line numbers and an uncommitted tree that then
changes makes every filed range wrong.

The adaptation pass gets a generous timeout (30 min by default, `--agent-timeout`
or `[profile.<name>.describe] timeout` to change it). It rewrites a whole tree, so
it is a bigger job than describing a diff — and a timeout is quiet, falling back to
the plain template, which reads as "the AI pass did nothing".

Stub tickets are grouped by a free `epic:<id>` tag, **not** the reserved
`parent:<id>`. `parent:` makes a ticket self-driving — the resolver auto-approves
its plan and goes straight to implementing — which is wrong for a backlog you
intend to write by hand.

### `--guided`: a project shaped like a class assignment

`--guided` turns a scaffold into coursework. On top of the stubs and their
tickets it writes **`ASSIGNMENT.md`** — learning goals, the brief, ordered
milestones with a "Done when" for each, a rubric, and the project's real run
commands — and rewrites the ticket bodies as exercises instead of echoing the
marker text.

```bash
stingray scaffold fastapi-spa ./hw3 --guided \
    --intent "a library loan tracker" --course-level intro --milestones 5
```

| flag | effect |
|---|---|
| `--guided` | write the handout and exercise-style ticket bodies |
| `--course-level intro\|intermediate\|advanced` | how much of the design the handout gives away (default `intermediate`) |
| `--milestones N` | how many milestones to group the stubs into (default 4, capped at the stub count) |
| `--no-assignment` | skip the file; the epic still carries the coursework |

**`ASSIGNMENT.md` is gitignored, on purpose.** The handout is coursework, not
code: a learner pushing their work shouldn't publish the brief, and an instructor
should be able to hand out a different one against the same skeleton. So it never
enters a commit — and because it could therefore be lost, everything down to the
rubric is mirrored into the epic ticket's description, which is the copy that
survives.

The handout comes from a second agent pass over the finished tree, using the same
`[describe]` agent/model/timeout as `--intent`. It is best-effort like every other
agent pass: a timeout, a missing agent, or `--no-ai` all fall back to a handout
generated from the scanned stubs and their `ACCEPTANCE:` lines. You always get a
handout; with an agent you get a better-written one.

The pass also leaves a scratch `.stingray-exercises.json` mapping each
`path:line` to that stub's ticket prose. It is read once and deleted — it never
reaches the project or a commit.

To stub a feature into a repo that **already has code**, don't use the CLI: file a
ticket with `/scaffold <what to build>` in its description and let the resolver do
it. Same convention, same backlog shape, applied to an existing codebase. See
`resolver/commands/README.md`.

### Templates

| name | shape |
|---|---|
| `python-cli` | argparse entry point, config loading, a core module |
| `fastapi-spa` | FastAPI routers/models/auth + a Vite React frontend |

`stingray scaffold --list-templates` prints them. The template is the *starting*
shape, not the ceiling — `--intent` lets the agent rename files, change
signatures and add modules, so `fastapi-spa` asked for a note app grew a
`search.py` and split its router three ways.

`fastapi-spa` renders ~11 stubs and typically adapts to ~30+, which brushes the
`--max-tickets` default of 30 — raise it if you want a ticket for every stub.

### Adding a template

Drop a directory into `stingray_cli/templates/<name>/` with a `template.toml`
(description + notes) and a `files/` tree. `{{project_name}}`, `{{package}}` and
`{{description}}` are substituted in both content and path segments; a `.tmpl`
suffix is stripped after substitution. Note that substitution matches the whole
`{{name}}` token, so JSX's `style={{...}}` passes through untouched.

Rendered trees are validated before they land: Python must `ast.parse`, JSON must
`json.loads`, and JS/JSX is checked for unclosed braces (a truncated agent edit).
That brace check is deliberately biased toward reporting *nothing* when anything
is ambiguous — strings, template literals, regexes and comments are all skipped —
because a false positive silently discards the adaptation and falls back to the
plain template.

## Layout

- `stingray_client/` — the shared library: `StingrayClient` and the ticket
  payload helpers. Depends only on `requests`. The resolver imports this too
  (`resolver/stingray.py` subclasses the client to add its audit logging).
- `stingray_cli/` — argparse front end, credentials, git plumbing, describe pass,
  scaffolding.
- `stingray_cli/station/` — the station: `inventory` (the TOML of intent),
  `identity` (reading `.env.<name>`), `units` (driving `systemctl --user`) and
  `status` (the join of systemd, git, logs and the server).

## Tests

```bash
cd cli && python -m pytest -q
```
