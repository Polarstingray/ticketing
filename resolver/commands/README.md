# Standard commands

This directory holds the resolver's library of **premade prompts** ("standard
commands"). A ticket invokes one by putting a single slash-command line in its
body (or a human comment), e.g.:

```
/security-audit

Focus on the new auth router.
```

When the resolver sees `/security-audit`, it loads `security-audit.md` from this
directory, injects its body as the ticket's **primary objective**, and runs the
normal lifecycle. The ticket's own title/description (the "Focus on…" line above)
are kept as supporting context. One library applies to **every** project the
resolver works — define a recurring cross-project task once here.

## File format

Each command is one Markdown file, `<name>.md`, where `<name>` is the slug used
after the slash (lowercase letters, digits, hyphens). It has a small frontmatter
block followed by the prompt body:

```markdown
---
type: code_review        # code_review | task   (default: task)
description: One-line summary, shown in error listings
priority: high           # optional hint, not enforced
---
The premade prompt text. This becomes the ticket's objective.
```

- **`type`** controls routing:
  - `code_review` → read-only review lifecycle (findings posted, no PR). Works
    even on a ticket whose own `type` is `task`.
  - `task` → plan → (approve) → implement lifecycle, opening a PR.
- **`description`** is shown when someone invokes an unknown command, so the
  resolver can list what's available.
- **`priority`** is an optional, non-enforced hint.

The frontmatter parser is intentionally minimal (flat `key: value` lines only) so
the resolver has no YAML dependency.

## Commands with follow-up code

Most commands are pure prompt — the library file is the whole feature. One is not:

- **`/scaffold`** has a post-implement hook (`resolver/scaffold_followup.py`,
  called from `do_implement`). The prompt has the agent stub a feature into the
  repo and write an `ASSIGNMENT.md` handout; the hook then lifts the handout out
  of the worktree before the commit (it is gitignored, so it could never ride the
  PR) and scans the finished tree to file one exercise ticket per
  `STINGRAY-STUB` marker, in the files the run actually touched.

  This means **`Command.name` is load-bearing for `scaffold`**: the hook keys off
  `command.name == "scaffold"`. Renaming the file detaches the follow-up and
  leaves the prompt filing nothing.

## Reserved names

`/ticket`, `/approve`, `/revise`, and `/review` are reserved control verbs and are
never treated as standard commands.

## Adding a command

1. Drop a new `<name>.md` here following the format above.
2. That's it — it's picked up on the next sweep. Consider adding a test in
   `tests/` if the routing matters.
