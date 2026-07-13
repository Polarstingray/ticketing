# The AI resolver: design notes

The resolver is an optional, headless companion that turns a bot-assigned ticket
into a reviewed pull request. It is the most interesting part of this project, so
this doc explains **what it does, why it's built the way it is, and the tradeoffs
that shaped it** — the decisions, not the line-by-line code.

> TL;DR — a cron-swept agent loop that plans → (human approves) → implements in an
> isolated git worktree → verifies with your test command → opens a PR, recording
> the token/USD cost of every phase back onto the ticket. It talks only to the
> public REST API with an ordinary API key, and every execution is sandboxed to an
> allowlisted repo.

## Design goals

1. **No privileged backdoor.** The resolver is just another API client. It logs in
   with an `X-API-Key`, is a non-admin `member`, and can only touch tickets assigned
   to it. All trust is expressed in data the backend already enforces, not in a side
   channel.
2. **A human stays in the loop by default.** The agent proposes a plan and waits for
   `/approve` before it writes code. Skipping that gate (`dangerous` tag) is explicit
   and opt-in.
3. **Blast radius is bounded.** Code execution happens in a throwaway `git worktree`,
   and the target repo must resolve inside an allowlisted `PROJECTS_ROOT`. Your live
   checkouts are never touched.
4. **The work is visible and costed.** Agent runs are otherwise invisible; the
   resolver writes each phase's model, tokens, and USD cost back to the ticket.
5. **Provider-agnostic.** Claude Code is the default runner, but the loop is written
   against an `agents.py` runner interface so opencode (and thus Gemini, Groq, local
   models, …) drop in via config.

## The lifecycle

```
file ticket (assigned to bot)
   └─ PLAN (read-only) ─→ post plan, reassign to human ──┐
                                                         │  /approve
   human approves ──────────────────────────────────────┘
   └─ IMPLEMENT (in git worktree) ─→ VERIFY (run tests) ─→ open PR
                                                         └─→ post PR + cost, reassign to human
```

State lives entirely in the ticket, encoded as **reserved control tags**
(`claude:planning`, `claude:implementing`, …) that only a trusted bot may set. The
sweep is idempotent: it reads a ticket's current tag to decide the next action, so a
crash or a missed cron tick simply resumes on the next sweep. There is no separate
job queue to get out of sync with the tickets.

## Decisions worth calling out

### Isolation: worktree + allowlist, not sandboxed Bash
The implement phase gives the agent a broad toolset (`Edit Write Read Glob Grep Bash`)
because narrowing Bash is a false comfort — a compound `cd x && cmd` defeats a naive
command filter. Real isolation comes from two things the agent *can't* talk its way
around: it runs inside a fresh `git worktree`, and the target repo path must resolve
(symlinks followed) inside `PROJECTS_ROOT` or the sweep refuses it. Path-traversal and
symlink escapes are rejected before any agent runs.

### The plan gate — and when to skip it
Two review gates (approve-the-plan, then review-the-PR) is the safe default, but it's
friction for trivial changes. `dangerous` collapses it to one (implement straight to a
PR you still review). A subtler case is **delegated sub-tasks**: when a lead resolver
fans a ticket out to workers, per-task human approval doesn't scale, so a child ticket
(carrying a trusted `parent:<id>` tag) is auto-approved by a review AI instead. The
security boundary there is deliberate — autonomy keys off the `parent:` tag, which only
a trusted bot can set, **never** off a forgeable `resolver:*` tag.

### Verification gate: catch the agent's own regressions
An agent will happily produce a confident diff that breaks the build. If you set
`VERIFY_COMMAND` (e.g. `cd backend && .venv/bin/pytest -q`), the resolver runs it in the
worktree after implementing and, on failure, **re-invokes the agent with the test
output** up to `VERIFY_MAX_RETRIES` times before publishing the result flagged. The
gotcha this surfaced: the worktree is a *clean* checkout with no gitignored `.venv`/
`node_modules`, so the verify command has to be self-contained.

### Reliability against flaky/free model providers
Running on free-tier models taught the loop to be defensive:
- **Fallback chain.** `AGENT_FALLBACK_MODELS` is an ordered list; a run that fails
  (including provider `503`/overloaded) falls through each alternative before the
  ticket is handed back.
- **Quota backoff, not failure.** A run that fails specifically on a rate-limit/quota
  error *parks* the ticket (keeps it claimed, preserves its phase tag) and auto-retries
  after `QUOTA_BACKOFF_MINUTES`, instead of burning a human hand-off on a transient 429.
- **Attempt cap.** `MAX_ATTEMPTS` stops a genuinely broken ticket from re-spending
  tokens on every cron tick forever.
- **Short read-only timeouts.** Plan/review get a much smaller budget than implement,
  so a stalled model fails over quickly instead of holding the lock.

### Cost routing
Not every phase needs the strongest model. Read-only plan/review can run on a cheaper
model than implement (per-phase overrides), and when a plan self-assesses a ticket as
`easy`/`hard` the implement phase can swap tiers — or escalate a hard ticket to a more
capable resolver entirely (`ESCALATE_TO_USER_ID`). This is how a cheap "free" resolver
and an expensive "heavy" resolver cooperate on one backlog.

### Cost accounting as a first-class output
Each phase emits a `token_usage` audit event *and* an `AgentRun` row on the ticket
(model, input/output/cache tokens, USD). The UI rolls a ticket's own cost up with every
delegated child's, so "what did resolving this cost?" is answerable at a glance — the
difference between a demo toy and something you'd let loose on a real budget.

## What's deliberately out of scope
No multi-tenancy, no billing/key management (you supply your own provider keys), and no
attempt to move off SQLite. The resolver manages *tickets and PRs*, not infrastructure.

## Testing
The loop is unit-tested (`resolver/tests/`, 200+ tests) with the agent CLI and the
Stingray API mocked, so lifecycle logic — state transitions, tag gating, path-allowlist
enforcement, delegation, backoff — is covered without spending a cent on real model
calls. A separate **eval harness** (`resolver/eval/`) scores the *real* resolver against
golden tickets on pass-rate and $/ticket, so a prompt or routing change can be measured,
not guessed.
