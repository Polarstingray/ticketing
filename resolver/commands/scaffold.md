---
type: task
description: Stub out a feature as a guided exercise, with a ticket per stub
priority: medium
---
Carve a GUIDED PROGRAMMING EXERCISE out of this repository. The learner will
implement it by hand; your job is the skeleton and the coursework around it, not
the solution. Write STUBS ONLY. Do not implement the feature.

What to build is stated in the ticket's own title and description below. Read it
as a brief from an instructor: it says what the exercise should cover.

Work in this order.

**1. Read before you write.** This repo already has code. Find the modules the
feature belongs next to, and match what you find: the existing layout, naming,
import style, error handling, and test framework. A skeleton that doesn't look
like the rest of the codebase teaches the wrong lesson.

**2. Write the skeleton.** Create or extend the files the feature needs, with
every non-trivial function body left as a stub in exactly this form:

    # STINGRAY-STUB: <what to implement, one line>
    # ACCEPTANCE: <how to know it is done>
    raise NotImplementedError("STINGRAY-STUB")

Use the target language's comment syntax and its equivalent of `raise` (JS/TS:
`throw new Error("STINGRAY-STUB")`; Go: `panic("STINGRAY-STUB")`).

Rules for the skeleton:

- Leave **at least three** stubs, and no more than about fifteen. Each stub is one
  ticket and should be one sitting's work.
- The `STINGRAY-STUB:` line says *what* and *why*. The `ACCEPTANCE:` line states an
  observable, testable outcome — the learner should be able to write the test from
  it. Both are one line each; wrap onto continuation comment lines if needed.
- Do NOT implement business logic, and do not leave a working version behind a
  flag or in a comment. No solutions in docstrings, no pseudocode, no "reference
  implementation" the learner can copy.
- Everything around the stubs must be real and correct: signatures, type hints,
  imports, routes, wiring, packaging, and any new module's place in the existing
  entry points. The tree must still import cleanly and the existing test suite
  must still collect and pass.
- If tests belong with the feature, add the test *file* with its cases stubbed the
  same way, so the learner writes assertions rather than scaffolding.
- Touch as little existing code as possible. Wire the new surface in; do not
  refactor what's already there.

**3. Write the handout.** Create `ASSIGNMENT.md` at the repository root with these
sections, in this order:

    # <feature name>
    ## Learning goals            3-6 bullets, transferable skills
    ## What you are building     1-2 paragraphs, the brief as a client would state it
    ## Milestones                ordered; each has a goal, its stubs as `path:line`
                                 bullets, and a "**Done when:**" observable outcome
    ## Rubric                    a table of criteria against weights summing to 100
    ## Getting started           this project's real install/run/test commands

Then add `ASSIGNMENT.md` to the repository's `.gitignore` (create the file if it
has none). The handout is coursework, not code: the resolver reads it back and
posts it on this ticket, so it reaches the learner without landing in the commit.
Describe *what* and *why* throughout — never *how*. No code snippets in the
handout.

**4. Do not file tickets.** The resolver scans the finished tree and files one
ticket per `STINGRAY-STUB` marker itself, linked back to this ticket as their
epic. Filing them yourself would double them up.

Your final message should say what the skeleton covers, list the stubs you left as
`path:line — summary`, and name anything a learner will need that you could not
provide.
