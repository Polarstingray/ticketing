---
type: code_review
description: Security audit of the target repository
priority: high
---
Perform a focused SECURITY AUDIT of this repository. This is a read-only review:
report findings, do not change code.

Examine the code for security vulnerabilities, prioritizing exploitable issues in
code reachable from untrusted input. Look specifically for:

- Injection: SQL/NoSQL injection, OS command injection, template injection, and
  unsafe `eval`/`exec`/deserialization (`pickle`, `yaml.load`, etc.).
- AuthN/AuthZ: missing or incorrect authentication/authorization checks, broken
  access control, privilege escalation, IDOR, and missing ownership checks on
  mutating endpoints.
- Secrets: hardcoded credentials, API keys, or tokens; secrets logged or returned
  in responses; weak or missing secret management.
- Web: XSS (stored/reflected/DOM), CSRF, SSRF, open redirects, and unsafe CORS.
- Path/file: path traversal, arbitrary file read/write, and unsafe archive
  extraction.
- Crypto & data handling: weak/hardcoded crypto, missing TLS verification, unsafe
  randomness for security purposes, and sensitive data exposure.
- Input validation: missing validation/sanitization on external input, mass
  assignment, and unbounded resource use (no limits → DoS).
- Dependencies & config: obviously outdated/vulnerable dependencies and insecure
  default configuration.

For each finding report: a short title, severity (critical/high/medium/low), the
exact file and line range, why it is exploitable, and a concrete remediation. If
you find no issues in a category, say so briefly rather than padding. Ground every
finding in code you actually read — do not speculate about code you have not seen.
