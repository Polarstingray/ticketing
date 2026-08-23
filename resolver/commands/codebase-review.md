---
type: task
description: File one code-review ticket per significant feature in the target repo
priority: medium
---
Map this repository into its significant FEATURES and file one code-review ticket
per feature. You are producing a reading guide for someone who needs to understand
this codebase — you are not fixing anything and not writing code.

The ticket's own title and description are the brief. If they name a single
feature ("only the auth flow", "just the resolver sweep"), scope yourself to that
one feature and file a single ticket for it. Otherwise cover the whole codebase.

**Teach Mode.** If the ticket description or any human comment contains the word
`teach` (or `--teach`), write every description as a mentor explaining the system
to a student who is learning from it, rather than as a note to a peer who already
knows it. In teach mode each description should cover:

- What the feature does, in plain language, before any code detail.
- The *why* behind the design — what constraint or failure mode led here, and
  what the obvious-but-wrong alternative would have been.
- How the feature connects to the rest of the system: what calls into it, what it
  calls, and where its data comes from and goes.
- Patterns worth studying and generalizing beyond this repo.
- The non-obvious details — the line that looks redundant but isn't, the ordering
  that matters, the edge case being defended against.
- Two or three questions the student should be able to answer after reading it.

Without teach mode, keep descriptions factual and reviewer-oriented: what the
feature is, which files implement it, and what a reviewer should scrutinize.

Work in this order.

**1. Enumerate the code.** Run `git ls-files` (or list the tree) to see every
tracked file. Skip anything that isn't worth reviewing: lock files, generated or
minified assets, images and other binaries, vendored dependencies, and pure
scaffolding such as empty `__init__.py` files or IDE configuration. Configuration
files are context, not features — read them, but do not file tickets about them.

**2. Group files into features.** A feature is a cluster of files that together
implement one user-visible capability or one significant internal subsystem —
an auth flow, a ticket lifecycle, a diff-to-code-blocks pipeline, a CLI surface,
a background sweep. Judge by what the code *does*, not by directory layout: a
feature routinely spans a router, a service, a model and a component. Read enough
of each cluster to describe it accurately; do not infer a feature's behaviour from
its filename.

Aim for between 3 and 10 features. A small repo may honestly have three; a large
one has more than ten, so pick the ten that a newcomer most needs to understand
and say in the summary which areas you left out.

**3. File one ticket per feature.** For each feature, run the resolver's filer
from the repo root — do not hand-write `curl`:

    resolver/file_ticket.py \
      --type code_review \
      --title "Review: <feature name>" \
      --description "<the prose from above>" \
      --parent <this ticket's id> \
      --code-block <path>:<language>:<start>-<end> [--code-block ...]

Notes on the invocation:

- `--parent` is required. It links each child to this ticket, makes the child
  self-driving, and inherits this ticket's `repo:`, `rev:` and `branch:` tags so
  the child is reviewed against the same commit. Do not pass `repo:`, `rev:` or
  `branch:` yourself.
- Attach one to three `--code-block` ranges per ticket, pointing at the most
  representative code in the feature — the entry point and the core logic, not
  every file it touches. Verify the line numbers against the file on disk; the
  filer reads those exact lines, so an off-by-one quotes the wrong code.
- Keep each block under ~200 lines. Reference the remaining files by path in the
  description instead of quoting them.
- Set `--priority` from how central the feature is to understanding the system,
  not from risk.

**4. Post a summary comment on this ticket.** List every child ticket you filed
with its id and feature name, note which parts of the codebase you deliberately
left uncovered and why, and state whether teach mode was on. If you could not file
some ticket, say so explicitly rather than silently dropping the feature.
