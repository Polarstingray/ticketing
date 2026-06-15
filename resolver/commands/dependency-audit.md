---
type: code_review
description: Audit third-party dependencies for risk and staleness
priority: medium
---
Perform a read-only DEPENDENCY AUDIT of this repository. Report findings; do not
change code.

Inspect the project's dependency manifests and lockfiles (e.g. `requirements*.txt`,
`pyproject.toml`, `package.json`/`package-lock.json`, `go.mod`, `Gemfile.lock`).
For the declared dependencies, assess:

- Known-vulnerable or end-of-life versions, and pins that are far behind current
  releases.
- Unpinned or loosely-pinned versions that make builds non-reproducible.
- Direct dependencies that appear unused, and heavyweight dependencies pulled in
  for trivial functionality that the standard library or existing deps cover.
- Licensing or supply-chain red flags (abandoned packages, typosquat-like names,
  packages installed from non-standard sources).

For each finding report: the package and version, the concern, severity
(critical/high/medium/low), and a concrete recommended action (upgrade target,
pin, remove, or replace). Be specific and cite the manifest file and line where
the dependency is declared. Do not invent CVEs — if you are unsure whether a
version is vulnerable, say so and recommend verification.
