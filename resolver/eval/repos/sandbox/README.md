# sandbox

A minimal fixture project for the resolver eval harness — a tiny `calc` module with a
pytest suite. The harness copies this directory into a throwaway `PROJECTS_ROOT`,
`git init`s it, and points eval cases at it via a `repo:sandbox` tag. Keep it small and
self-contained (no third-party deps beyond pytest).
