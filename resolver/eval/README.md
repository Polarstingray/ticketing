# Resolver evaluation harness

Measures the resolver's **implement capability**: runs the real, unmodified
`resolve_tickets.py` against a set of golden tickets in a throwaway sandbox and scores
each by an **independent acceptance test**. Turns "this prompt/model feels better" into a
pass-rate and a dollar figure you can compare.

## What it does

For each run, in a temp sandbox (auto-cleaned unless `--keep`):

1. **Temp backend** — seeds a temp SQLite DB with an admin + bot user and API keys, then
   runs the real `uvicorn` app against it (`harness/backend.py`). The resolver talks to
   it over HTTP exactly as it does in production.
2. **Seed** — copies each fixture repo from `repos/` into a temp `PROJECTS_ROOT`,
   `git init`s it, and creates one ticket per case assigned to the bot.
3. **Drive** — invokes `resolve_tickets.py --ticket <id>` per sweep and plays the human:
   when the resolver hands a ticket back awaiting plan approval, it posts `/approve` and
   reassigns to the bot, until the ticket is implemented or fails (`harness/driver.py`).
4. **Score** — checks the resolver's output branch (`claude/ticket-<id>`) out into a
   scratch worktree, drops in the case's **harness-owned** acceptance test (the agent
   never sees it), runs it, and rolls up cost/tokens from the `AgentRun` records
   (`harness/score.py`).
5. **Report** — writes `reports/eval-<ts>.json`, prints a table, and (with `--baseline`)
   diffs against a previous report, exiting non-zero if anything regressed
   (`harness/report.py`).

## Running

Use the resolver venv (it has `uvicorn`/`fastapi`/`yaml`/`pytest`):

```bash
cd resolver

# all cases against the Claude resolver (costs tokens — real agent runs)
.venv/bin/python eval/run_eval.py --agent claude --model claude-opus-4-8

# one case, keep the sandbox to inspect
.venv/bin/python eval/run_eval.py --case add-multiply --keep

# the free/opencode resolver
.venv/bin/python eval/run_eval.py --agent opencode --model groq/llama-3.3-70b-versatile

# regression-gate against a saved baseline
.venv/bin/python eval/run_eval.py --baseline eval/reports/eval-<ts>.json
```

The resolver subprocess needs the agent CLI (`claude` / `opencode`) on PATH and
authenticated, the same as a real run.

## Adding a case

Drop a YAML file in `cases/`:

```yaml
id: my-case                     # stable id (report key)
title: "..."
description: "what the agent should do"
type: task                      # task | code_review
priority: medium
repo: sandbox                   # a dir under repos/
acceptance:
  command: "python -m pytest tests/test_acceptance.py -q"
  files:                        # written into the scoring worktree (ground truth)
    tests/test_acceptance.py: |
      from calc import multiply
      def test_it():
          assert multiply(3, 4) == 12
```

Keep the acceptance test independent of any test the agent might write — it is the
objective judge. Add new fixture projects under `repos/<name>/` (small, self-contained,
a root `conftest.py` so `from <module> import ...` works).

## Self-test (free, no agent)

```bash
.venv/bin/python -m pytest eval/tests/ --rootdir=$PWD -q -p no:cacheprovider
```

Stands up the backend, pre-creates good/bad output branches, and asserts the scorer +
cost rollup behave — no model spend. Kept out of the main `tests/` suite so it doesn't
slow ordinary resolver test runs.

## Scope (v1)

Implement-capability only: task tickets driven plan→approve→implement, scored by
acceptance test. Out of scope for now: LLM-as-judge quality scoring, first-class
review/escalation tracks, the GitHub/PR path (scores off the local branch), and parallel
execution.
