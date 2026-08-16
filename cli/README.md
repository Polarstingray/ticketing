# `stingray` — the Stingray Tickets CLI

Files code-review tickets straight from git, optionally writes their prose with a
local AI agent, and scaffolds new projects with a ready-made backlog. Every
ticket it files carries a `repo:<name>` tag, which is what lets you assign it to
a resolver bot and press **Apply fixes**.

## Install

```bash
pipx install ./cli          # from a checkout
stingray auth login --url http://localhost:3000 --bot-user-id 2
```

`auth login` prompts for an API key (never echoed) and validates it before
storing. Credentials live in `~/.config/stingray/config.toml` at mode 0600.

> **The key needs the `cli` scope.** `repo:` is a reserved control tag; only an
> admin can mint a scoped key, from **Profile → API keys**. Without it the CLI
> can still file tickets, but they won't carry a repo tag and the resolver won't
> be able to check the code out.

## Commands

```bash
stingray review                      # last commit + working tree
stingray review HEAD~3..HEAD         # an explicit range
stingray review my-branch            # merge-base(main, branch)..branch
stingray review --staged             # what you're about to commit
stingray review --describe           # let a local agent write the prose
stingray review --assign-bot -y      # file it straight at the resolver

stingray file --type task --title "Flaky retry test" --priority low
stingray scaffold python-cli ./newproj --intent "a log parser"
stingray auth status
```

### Credential precedence

Highest first: `--url` / `--api-key` flags, then `STINGRAY_URL` /
`STINGRAY_API_KEY` in the environment, then the selected profile.

This is deliberately the **opposite** of `resolver/config.py`, where the `.env`
file wins over the ambient environment. The resolver is a daemon whose identity
must not shift with whatever a shell exported; an interactive CLI should honor an
explicit override. Don't "fix" one to match the other.

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

### Adding a template

Drop a directory into `stingray_cli/templates/<name>/` with a `template.toml`
(description + notes) and a `files/` tree. `{{project_name}}`, `{{package}}` and
`{{description}}` are substituted in both content and path segments; a `.tmpl`
suffix is stripped after substitution.

## Layout

- `stingray_client/` — the shared library: `StingrayClient` and the ticket
  payload helpers. Depends only on `requests`. The resolver imports this too
  (`resolver/stingray.py` subclasses the client to add its audit logging).
- `stingray_cli/` — argparse front end, credentials, git plumbing, describe pass,
  scaffolding.

## Tests

```bash
cd cli && python -m pytest -q
```
