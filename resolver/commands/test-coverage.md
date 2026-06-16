---
type: task
description: Add tests to cover untested or under-tested behavior
priority: medium
---
Improve this repository's automated TEST COVERAGE. This is an implementation task:
add tests, and only touch non-test code where a small change is needed to make
something testable.

First identify the most valuable gaps: core logic, error/edge-case paths, and
recently changed code that has little or no coverage. Then add focused,
deterministic tests for them using the project's existing test framework and
conventions (match the style of the tests already in the repo).

Guidelines:
- Prefer a handful of high-value tests over many trivial ones; cover real behavior
  and failure modes, not just the happy path.
- Do not test third-party libraries or rewrite existing passing tests.
- Keep tests fast and hermetic — no network, no real clocks, no sleeps; use the
  project's existing fixtures/mocks.
- Run the test suite and make sure everything (new and existing) passes before you
  finish.

Summarize which behaviors you added coverage for and report the final test results.
