#!/usr/bin/env python3
"""Stingray ticket resolver.

Sweeps for tickets assigned to the `claude-bot` user, runs a headless Claude
Code instance to (by default) propose a plan and — once a human approves with
`/approve` — implement it on a branch and open a PR. Tickets tagged `dangerous`
skip the plan gate and go straight to a PR.

All code execution happens in an isolated `git worktree`, so the user's live
checkout under PROJECTS_ROOT is never modified.

Run one sweep:        python resolve_tickets.py
Process one ticket:   python resolve_tickets.py --ticket 42
See actions only:     python resolve_tickets.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import agents
import audit
import file_ticket
from config import Config, RepoNotAllowed, RepoNotFound
from stingray import StingrayClient

HERE = Path(__file__).resolve().parent

# --- tag conventions -----------------------------------------------------
CLAUDE_PREFIX = "claude:"
TAG_PLANNING = "claude:planning"            # plan run in flight
TAG_AWAIT_PLAN = "claude:awaiting-plan-approval"
TAG_IMPLEMENTING = "claude:implementing"    # implement run in flight
TAG_AWAIT_PR = "claude:awaiting-pr-review"
TAG_DANGEROUS = "dangerous"
REPO_TAG_PREFIX = "repo:"

PLAN_MARKER = "📋 **Proposed plan**"
IMPL_MARKER = "✅ **Implemented**"
FAIL_MARKER = "⚠️ Resolver could not complete"
FILED_MARKER = "🎫 Filed from `/ticket`"
WORK_DIR = Path(__file__).resolve().parent / "work"

# Per-event audit truncation, set from config at sweep start (see main()).
AUDIT_TAIL_BYTES = 4096

# Tags counting failed plan/implement attempts (see process()/bump_attempts).
ATTEMPT_PREFIX = "claude:attempt-"


def log(msg: str) -> None:
    """Human-readable INFO line — fans out to stdout (cron.log) and sweep log."""
    audit.get_logger().info(msg)


def phase(name: str, ticket: dict, message: str, **extra) -> None:
    """Record a state transition as a `phase` audit event (and INFO line)."""
    audit.audit_event(audit.get_logger(), "phase", message,
                      phase=name, ticket_id=ticket.get("id"), **extra)


def _killpg(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group, falling back to the child."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _cmd_category(cmd: list[str]) -> str:
    exe = os.path.basename(cmd[0]) if cmd else ""
    if exe.startswith("git"):
        return "git"
    if exe == "gh":
        return "gh"
    if "claude" in exe:
        return "claude"
    return "bash"


def _audit_subprocess(cmd: list[str], cwd, *, rc, start: float, output: str,
                      **extra) -> None:
    category = _cmd_category(cmd)
    audit.audit_event(
        audit.get_logger(), "subprocess",
        f"{category}: {' '.join(cmd)} -> rc={rc}",
        level=logging.DEBUG,
        argv=cmd,
        category=category,
        cwd=str(cwd) if cwd else None,
        rc=rc,
        duration_ms=round((time.monotonic() - start) * 1000),
        output_tail=audit.clip(output, AUDIT_TAIL_BYTES),
        **extra,
    )


def run(cmd: list[str], cwd: str | Path | None = None, timeout: int | None = 120):
    """Run a command, capturing combined output. Returns (rc, output).

    Every invocation is recorded as a `subprocess` audit event (argv, cwd, rc,
    duration, output tail). The child is started in its own process group
    (`start_new_session=True`) so that on timeout we can kill the *whole tree*,
    not just the direct child — otherwise a server or subprocess Claude spawned
    (e.g. `uvicorn`) is orphaned and keeps running. On TimeoutExpired we re-raise
    with whatever output was captured so callers can log what hung."""
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as exc:
        # A missing binary (bad CLAUDE_BIN, no `gh`) used to propagate raw to the
        # sweep catch-all; surface it as a clean non-zero result instead.
        msg = f"{cmd[0]}: {exc}"
        _audit_subprocess(cmd, cwd, rc=127, start=start, output=msg, error=type(exc).__name__)
        return 127, msg
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _killpg(proc)
        out, _ = proc.communicate()
        _audit_subprocess(cmd, cwd, rc=None, start=start, output=out or "", timed_out=True)
        raise subprocess.TimeoutExpired(cmd, timeout, output=out)
    _audit_subprocess(cmd, cwd, rc=proc.returncode, start=start, output=out)
    return proc.returncode, out


# --- ticket helpers ------------------------------------------------------
def claude_tags(ticket: dict) -> set[str]:
    return {t for t in ticket.get("tags", []) if t.startswith(CLAUDE_PREFIX)}


def repo_name_of(ticket: dict) -> str | None:
    for t in ticket.get("tags", []):
        if t.startswith(REPO_TAG_PREFIX):
            return t[len(REPO_TAG_PREFIX):].strip()
    return None


def set_state(client: StingrayClient, ticket: dict, new_claude_tags: list[str],
              **fields) -> dict:
    """Replace the ticket's claude:* tags with new_claude_tags (preserving
    repo:/dangerous/other tags) and apply any other PATCH fields in one call."""
    kept = [t for t in ticket.get("tags", []) if not t.startswith(CLAUDE_PREFIX)]
    return client.update_ticket(ticket["id"], tags=kept + new_claude_tags, **fields)


def render_code_blocks(ticket: dict) -> str:
    blocks = ticket.get("code_blocks") or []
    if not blocks:
        return ""
    parts = ["\nRelevant code (flagged by the reporter):"]
    for b in blocks:
        loc = f"{b.get('filename')}:{b.get('line_start')}-{b.get('line_end')}"
        lang = b.get("language", "")
        parts.append(f"\n{loc}\n```{lang}\n{b.get('content','')}\n```")
    return "\n".join(parts)


def find_approved_plan(comments: list[dict], bot_id: int) -> str | None:
    """The most recent bot comment that carries the plan marker."""
    for c in reversed(comments):
        if c.get("author") == bot_id and PLAN_MARKER in (c.get("body") or ""):
            return c["body"]
    return None


def latest_human(comments: list[dict], bot_id: int) -> dict | None:
    """The most recent comment not authored by the bot (comments oldest-first)."""
    for c in reversed(comments):
        if c.get("author") != bot_id:
            return c
    return None


def recent_failures(comments: list[dict], bot_id: int) -> int:
    """Count consecutive trailing resolver-failure comments — i.e. failures
    since the last *successful* phase (a posted plan or an implemented PR). This
    naturally resets the attempt counter once the resolver makes progress, while
    a ticket that keeps failing accumulates toward the MAX_ATTEMPTS cap."""
    n = 0
    for c in reversed(comments):
        if c.get("author") != bot_id:
            continue
        body = c.get("body") or ""
        if PLAN_MARKER in body or IMPL_MARKER in body:
            break  # a successful phase resets the streak
        if FAIL_MARKER in body:
            n += 1
    return n


# --- /ticket directive ---------------------------------------------------
# A human can ask the resolver to file a NEW ticket by writing a `/ticket` line
# in the ticket body or a comment. Unlike file_ticket.py (which the implement
# agent runs), this is parsed deterministically by the resolver — the model is
# never in the loop, so the type/priority/auth can't be gotten wrong.
_KEY_RE = re.compile(r"\[key:([0-9a-f]{10})\]")


class _DirectiveError(ValueError):
    """A /ticket directive that couldn't be parsed/validated. The message is
    surfaced back on the ticket so the author can fix it."""


class _DirectiveParser(argparse.ArgumentParser):
    """argparse that raises instead of calling sys.exit, so one bad directive
    can't abort the sweep."""

    def error(self, message: str):  # noqa: D401 - argparse hook
        raise _DirectiveError(message)


def _directive_parser() -> _DirectiveParser:
    p = _DirectiveParser(prog="/ticket", add_help=False)
    p.add_argument("type", choices=file_ticket.TYPES)
    p.add_argument("title")
    p.add_argument("--priority", default="medium", choices=file_ticket.PRIORITIES)
    p.add_argument("--description", default="")
    p.add_argument("--tag", action="append")
    p.add_argument("--assign", type=file_ticket.user_id)
    p.add_argument("--code-block", action="append", dest="code_block")
    return p


def body_is_directive_only(ticket: dict) -> bool:
    """True when the ticket description is nothing but `/ticket` directive line(s)
    (and whitespace) — i.e. a pure filing request with no real work to plan."""
    nonempty = [l.strip() for l in (ticket.get("description") or "").splitlines() if l.strip()]
    return bool(nonempty) and all(
        l == "/ticket" or l.startswith("/ticket ") for l in nonempty
    )


def directive_key(line: str) -> str:
    """Stable id for a directive line, used to file it exactly once across sweeps."""
    return hashlib.sha1(line.strip().encode("utf-8")).hexdigest()[:10]


def collect_directives(ticket: dict, comments: list[dict], bot_id: int) -> list[dict]:
    """Find every `/ticket ...` line in the ticket body and human comments.
    Returns dicts of {key, line, args, author}. Bot-authored text (incl. our own
    marker comment) is ignored so the resolver never parses its own output."""
    sources: list[tuple[str, int | None]] = [
        (ticket.get("description") or "", ticket.get("created_by"))
    ]
    for c in comments:
        if c.get("author") != bot_id:
            sources.append((c.get("body") or "", c.get("author")))

    found: list[dict] = []
    seen: set[str] = set()
    for text, author in sources:
        for raw in text.splitlines():
            line = raw.strip()
            if line != "/ticket" and not line.startswith("/ticket "):
                continue
            key = directive_key(line)
            if key in seen:
                continue  # identical directive twice in one sweep -> file once
            seen.add(key)
            found.append({
                "key": key,
                "line": line,
                "args": line[len("/ticket"):].strip(),
                "author": author,
            })
    return found


def already_handled_keys(comments: list[dict], bot_id: int) -> set[str]:
    """Directive keys the resolver already acted on, read back from its own
    marker comments — this is what makes a body directive file only once."""
    keys: set[str] = set()
    for c in comments:
        if c.get("author") == bot_id and FILED_MARKER in (c.get("body") or ""):
            keys.update(_KEY_RE.findall(c.get("body") or ""))
    return keys


def directive_payload(directive: dict, repo: Path) -> dict:
    """Parse one directive into a create_ticket payload, reusing file_ticket's
    validation + on-disk code-block reading. Raises _DirectiveError on bad input."""
    try:
        tokens = shlex.split(directive["args"])
    except ValueError as exc:
        raise _DirectiveError(f"could not parse arguments: {exc}")
    args = _directive_parser().parse_args(tokens)
    args.root = str(repo)
    try:
        payload = file_ticket.build_payload(args)
    except ValueError as exc:
        raise _DirectiveError(str(exc))
    # Default the new ticket to the directive's author so the requester can find
    # it; an explicit --assign wins (build_payload already set assigned_to then).
    if "assigned_to" not in payload and directive["author"] is not None:
        payload["assigned_to"] = directive["author"]
    return payload


def handle_ticket_directives(cfg: Config, client: StingrayClient, ticket: dict,
                             comments: list[dict], repo: Path, dry_run: bool) -> None:
    """File any new `/ticket` directives on this ticket (once each), then record
    what happened in a single marker comment so the next sweep skips them."""
    directives = collect_directives(ticket, comments, cfg.bot_user_id)
    if not directives:
        return
    done = already_handled_keys(comments, cfg.bot_user_id)
    pending = [d for d in directives if d["key"] not in done]
    if not pending:
        return

    if dry_run:
        log(f"#{ticket['id']}: would file {len(pending)} /ticket directive(s)")
        return

    lines: list[str] = []
    for d in pending:
        try:
            payload = directive_payload(d, repo)
            created = client.create_ticket(**payload)
            lines.append(
                f"- #{created['id']} — {payload['type']} \"{created.get('title')}\""
                f"  [key:{d['key']}]"
            )
            log(f"#{ticket['id']}: /ticket filed #{created['id']} ({d['key']})")
        except _DirectiveError as exc:
            lines.append(f"- error: {exc}  `{d['line']}`  [key:{d['key']}]")
            log(f"#{ticket['id']}: /ticket rejected ({d['key']}): {exc}")
        except Exception as exc:  # API failure etc. — report, don't crash the sweep
            lines.append(f"- error filing: {exc}  `{d['line']}`  [key:{d['key']}]")
            log(f"#{ticket['id']}: /ticket failed ({d['key']}): {exc}")

    client.add_comment(ticket["id"], f"{FILED_MARKER}\n\n" + "\n".join(lines))


# --- agent runners -------------------------------------------------------
def _stream_subprocess(cmd: list[str], cwd: Path, timeout: int, log_path: Path,
                       on_line, *, category: str, label: str):
    """Spawn `cmd`, stream stdout line-by-line into `on_line(stripped_line)`, tee
    the raw output verbatim to `log_path`, and enforce `timeout`. Returns
    `(rc, timed_out, launch_err)`; on a launch failure rc is None and launch_err
    is the exception. Shared by every agent runner so they get identical
    kill/reap/timeout/audit behavior — only the per-line parsing differs."""
    logger = audit.get_logger()
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True, bufsize=1,
        )
    except (FileNotFoundError, OSError) as exc:
        log_path.write_text(f"LAUNCH FAILED: {cmd[0]}: {exc}\n")
        return None, False, exc

    deadline = start + timeout
    timed_out = False
    with open(log_path, "w", encoding="utf-8") as raw:
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                line = proc.stdout.readline()
                if line == "":  # EOF
                    break
                raw.write(line)
                raw.flush()
                stripped = line.strip()
                if stripped:
                    on_line(stripped)
        finally:
            # Only kill on timeout. On a clean EOF the process is exiting on its
            # own; killing it here would make rc negative and mis-report a
            # successful run as a failure. Reap with a short grace period, then
            # force-kill only if it genuinely refuses to exit.
            if timed_out:
                _killpg(proc)
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _killpg(proc)
                rc = proc.wait()

    audit.audit_event(
        logger, "subprocess", f"{category} ({label}) -> rc={rc} timed_out={timed_out}",
        level=logging.DEBUG, category=category, argv=cmd[:2] + ["...", label], rc=rc,
        duration_ms=round((time.monotonic() - start) * 1000), timed_out=timed_out or None,
    )
    return rc, timed_out, None


def summarize_tool_use(name: str, inp: dict | None) -> str:
    """One-line, secret-free summary of a Claude tool call for the audit log."""
    inp = inp or {}
    if name in ("Read", "Write", "Edit", "MultiEdit"):
        return str(inp.get("file_path") or "")
    if name == "NotebookEdit":
        return str(inp.get("notebook_path") or "")
    if name == "Bash":
        return str(inp.get("command") or "")
    if name in ("Glob", "Grep"):
        pat = inp.get("pattern") or ""
        path = inp.get("path")
        return f"{pat} in {path}" if path else str(pat)
    if name in ("Task", "Agent"):
        return str(inp.get("description") or "")
    if name == "WebFetch":
        return str(inp.get("url") or "")
    # Unknown tool: compact preview of the first few keys (no values dumped raw).
    return json.dumps({k: inp[k] for k in list(inp)[:3]}, default=str)[:200]


def _claude_event(logger, ticket_id, line: str, result: dict) -> None:
    """Parse one stream-json line; audit any tool_use and capture the final
    result. `result` is mutated in place to hold the terminal result event."""
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return
    etype = evt.get("type")
    if etype == "assistant":
        for block in (evt.get("message", {}) or {}).get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "?")
                summary = summarize_tool_use(name, block.get("input"))
                audit.audit_event(
                    logger, "agent_tool", f"claude {name}: {summary}",
                    level=logging.DEBUG, agent="claude", tool=name,
                    input_summary=summary,
                )
    elif etype == "result":
        result.update(evt)


def run_claude(cfg: Config, prompt: str, cwd: Path, mode: str, log_path: Path) -> tuple[bool, str]:
    """Run headless Claude, streaming its output so every tool call (Read/Write/
    Edit/Bash/...) is recorded as an `agent_tool` audit event. The raw stream is
    teed verbatim to `log_path`. mode is 'plan' (read-only) or 'implement'.
    Returns (ok, result_text)."""
    cmd = [cfg.agent_bin, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if cfg.agent_model:
        cmd += ["--model", cfg.agent_model]
    if mode == "plan":
        # Read-only exploration. We deliberately do NOT use --permission-mode
        # plan: headless, that routes the plan through ExitPlanMode (which can't
        # be approved non-interactively), so the plan text never reaches the
        # result. Granting only read tools and asking for the plan as the final
        # message captures it cleanly while guaranteeing no edits.
        cmd += ["--permission-mode", "default", "--allowedTools", "Read", "Glob", "Grep"]
    else:
        cmd += ["--permission-mode", "acceptEdits"]
        if cfg.implement_tools:
            cmd += ["--allowedTools", *cfg.implement_tools.split()]

    # Implement does strictly more than plan (edit + verify), so it gets the
    # larger budget; plan stays snappy on the smaller one.
    timeout = cfg.agent_implement_timeout if mode == "implement" else cfg.agent_timeout
    logger = audit.get_logger()
    result: dict = {}
    rc, timed_out, launch_err = _stream_subprocess(
        cmd, cwd, timeout, log_path,
        lambda line: _claude_event(logger, None, line, result),
        category="claude", label=mode,
    )
    if launch_err is not None:
        return False, f"Could not launch Claude ({cmd[0]}): {launch_err}"
    if timed_out:
        return False, f"Claude timed out after {timeout}s."
    result_text = (result.get("result") or "").strip()
    if not result:
        # No terminal result event — likely a CLI/schema change. Surface it
        # rather than reporting a silent success.
        logger.warning("claude produced no result event (rc=%s) — output schema drift?", rc)
        return rc == 0, result_text or f"claude exited {rc} with no result event"
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        return False, result_text or f"claude error (subtype={result.get('subtype')})"
    return rc == 0, result_text


def _summarize_opencode_tool(tool: str, inp: dict | None) -> str:
    """One-line, secret-free summary of an opencode tool call for the audit log.
    opencode tool names are lowercase and its file inputs use `filePath`, so this
    can't reuse summarize_tool_use (which keys on Claude's tool names)."""
    inp = inp or {}
    if tool == "bash":
        return str(inp.get("command") or "")
    if tool in ("read", "write", "edit", "patch"):
        return str(inp.get("filePath") or inp.get("file_path") or inp.get("path") or "")
    if tool in ("grep", "glob"):
        pat = inp.get("pattern") or inp.get("query") or ""
        path = inp.get("path")
        return f"{pat} in {path}" if path else str(pat)
    if tool in ("list", "ls"):
        return str(inp.get("path") or "")
    if tool in ("webfetch", "fetch"):
        return str(inp.get("url") or "")
    # Unknown tool: compact preview of the first few keys (no values dumped raw).
    return json.dumps({k: inp[k] for k in list(inp)[:3]}, default=str)[:200]


def _opencode_event(logger, line: str, state: dict) -> None:
    """Parse one opencode `run --format json` (JSONL) event, auditing tool calls
    as `agent_tool` events and accumulating the final assistant text into `state`
    (mutated in place: `texts`, `step_texts`, `final`, `stopped`, `error`).

    Observed schema (verified against opencode 1.16.2) — each line has a top-level
    `type`: step_start | tool_use | text | step_finish | error. For step_finish the
    stop reason lives at `part.reason` (e.g. "stop"), not at the top level; tool
    events carry `part.tool` + `part.state.input`; text carries `part.text`.
    NOTE: opencode's --format json schema is not formally documented; re-verify if
    opencode changes it (mirrors the schema-drift guard in run_claude)."""
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return
    etype = evt.get("type")
    part = evt.get("part") or {}
    if etype == "tool_use":
        tool = part.get("tool", "?")
        inp = (part.get("state") or {}).get("input") or {}
        summary = _summarize_opencode_tool(tool, inp)
        audit.audit_event(
            logger, "agent_tool", f"opencode {tool}: {summary}",
            level=logging.DEBUG, agent="opencode", tool=tool, input_summary=summary,
        )
    elif etype == "text":
        text = part.get("text") or ""
        if text:
            state.setdefault("texts", []).append(text)
            state.setdefault("step_texts", []).append(text)
    elif etype == "step_start":
        # A new step's text supersedes the prior step's; the final answer is the
        # text emitted in the step that ends with reason=stop.
        state["step_texts"] = []
    elif etype == "step_finish":
        # The stop reason is on the part (part.reason), not the top-level event;
        # keep the top-level read as a fallback against schema variance.
        if (part.get("reason") or evt.get("reason")) == "stop":
            state["stopped"] = True
            state["final"] = "".join(state.get("step_texts") or state.get("texts") or [])
    elif etype == "error":
        state["error"] = evt.get("error") or {"name": "unknown"}


def run_opencode(cfg: Config, prompt: str, cwd: Path, mode: str, log_path: Path) -> tuple[bool, str]:
    """Run headless opencode (`opencode run ... --format json`), auditing each tool
    call as an `agent_tool` event and teeing the raw JSONL to `log_path`.

    mode is 'plan' (read-only: the permission-restricted `plan` agent, with no
    --dangerously-skip-permissions, so it cannot edit/run bash) or 'implement'
    (the unrestricted `build` agent with permissions auto-approved so edits run
    headlessly — isolation comes from the per-ticket worktree + PROJECTS_ROOT
    allowlist, same rationale as Claude's broad Bash allowlist). Returns
    (ok, result_text)."""
    cmd = [cfg.agent_bin, "run", prompt, "--format", "json"]
    if cfg.agent_model:
        cmd += ["--model", cfg.agent_model]
    if mode == "plan":
        cmd += ["--agent", cfg.opencode_plan_agent]
    else:
        cmd += ["--agent", cfg.opencode_build_agent, "--dangerously-skip-permissions"]

    timeout = cfg.agent_implement_timeout if mode == "implement" else cfg.agent_timeout
    logger = audit.get_logger()
    state: dict = {}
    rc, timed_out, launch_err = _stream_subprocess(
        cmd, cwd, timeout, log_path,
        lambda line: _opencode_event(logger, line, state),
        category="opencode", label=mode,
    )
    if launch_err is not None:
        return False, f"Could not launch opencode ({cmd[0]}): {launch_err}"
    if timed_out:
        return False, f"opencode timed out after {timeout}s."
    if state.get("error"):
        err = state["error"]
        name = err.get("name") if isinstance(err, dict) else err
        return False, f"opencode error: {name}"
    result_text = (state.get("final") or "".join(state.get("texts") or [])).strip()
    if not state.get("stopped") and not result_text:
        # opencode exited without a stop event AND without any assistant output.
        # This is what a swallowed provider error looks like: e.g. Gemini returns
        # a 503 "model overloaded", opencode logs it internally but still exits 0.
        # Treat it as a failure — otherwise do_implement commits the empty tree and
        # misreports it as the agent having "produced no code changes". (A genuine
        # success always carries a stop event; this only fires on the broken case.)
        logger.warning("opencode produced no output (rc=%s, no stop event) — likely a "
                       "provider error; see ~/.local/share/opencode/log", rc)
        return False, (
            f"opencode produced no output (exited {rc} with no stop event). The "
            "model/provider call likely failed — e.g. an overloaded-model 503. "
            "Check the opencode logs under ~/.local/share/opencode/log."
        )
    return rc == 0, result_text


class ClaudeRunner(agents.AgentRunner):
    """Claude Code adapter — delegates to run_claude (defined above), which owns
    the stream-json parsing and audit wiring."""

    name = "claude"
    label = "Claude"

    def run(self, cfg: Config, prompt: str, cwd: Path, mode: str,
            log_path: Path) -> tuple[bool, str]:
        return run_claude(cfg, prompt, cwd, mode, log_path)


class OpenCodeRunner(agents.AgentRunner):
    """opencode adapter — delegates to run_opencode (defined above), which owns the
    JSONL parsing and audit wiring. Point it at a free/cheap model (e.g. Gemini)
    via AGENT_MODEL on its resolver's .env."""

    name = "opencode"
    label = "opencode"

    def run(self, cfg: Config, prompt: str, cwd: Path, mode: str,
            log_path: Path) -> tuple[bool, str]:
        return run_opencode(cfg, prompt, cwd, mode, log_path)


agents.register_runner(ClaudeRunner())
agents.register_runner(OpenCodeRunner())


def run_agent(cfg: Config, prompt: str, cwd: Path, mode: str,
              log_path: Path) -> tuple[bool, str]:
    """Dispatch one plan/implement phase to the configured agent runner."""
    return agents.get_runner(cfg.agent).run(cfg, prompt, cwd, mode, log_path)


# --- git / worktree ------------------------------------------------------
def has_origin(repo: Path) -> bool:
    return run(["git", "-C", str(repo), "remote", "get-url", "origin"])[0] == 0


def ref_exists(repo: Path, ref: str) -> bool:
    return run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])[0] == 0


def resolve_base(repo: Path) -> tuple[str, str]:
    """Determine where to branch the fix from and what the PR base branch is.

    Returns (base_ref, base_branch): `base_ref` is a ref guaranteed to exist
    (so `git worktree add` can't fail with 'invalid reference'); `base_branch`
    is the branch name a PR should target. We never assume `origin/<x>` exists —
    origin/HEAD is often unset, and the local checkout may be on a feature branch
    that was never pushed."""
    remote_default = None
    rc, out = run(["git", "-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if rc == 0 and out.strip():
        remote_default = out.strip().split("/", 1)[-1]
    elif has_origin(repo):
        for cand in ("main", "master"):
            if ref_exists(repo, f"origin/{cand}"):
                remote_default = cand
                break

    # Branch from the remote default tip when we have it (clean PR base),
    # otherwise from the local checkout's HEAD, which always exists.
    base_ref = f"origin/{remote_default}" if remote_default and ref_exists(repo, f"origin/{remote_default}") else "HEAD"
    rc, cur = run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"])
    base_branch = remote_default or (cur.strip() if rc == 0 and cur.strip() else "main")
    return base_ref, base_branch


def prepare_worktree(repo: Path, ticket_id: int, base_ref: str) -> tuple[Path, str]:
    """Create an isolated worktree on branch claude/ticket-<id>. Reuses the
    branch if it already exists (rework); otherwise creates it off base_ref."""
    WORK_DIR.mkdir(exist_ok=True)
    wt = WORK_DIR / f"ticket-{ticket_id}"
    branch = f"claude/ticket-{ticket_id}"
    # Clear any stale worktree from a previous crashed run.
    run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)])
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    run(["git", "-C", str(repo), "worktree", "prune"])

    branch_exists = run(["git", "-C", str(repo), "rev-parse", "--verify", branch])[0] == 0
    if branch_exists:
        rc, out = run(["git", "-C", str(repo), "worktree", "add", str(wt), branch])
    else:
        rc, out = run(["git", "-C", str(repo), "worktree", "add", "-B", branch, str(wt), base_ref])
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {out}")
    return wt, branch


def remove_worktree(repo: Path, wt: Path) -> None:
    run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)])
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)


# --- prompts -------------------------------------------------------------
def plan_prompt(ticket: dict, repo: Path, revise_notes: str | None) -> str:
    p = [
        f"You are resolving Stingray ticket #{ticket['id']} in the repository at {repo}.",
        "",
        f"Title: {ticket['title']}",
        f"Priority: {ticket.get('priority')}",
        "Description:",
        ticket.get("description") or "(none)",
        render_code_blocks(ticket),
        "",
        "Produce a clear, step-by-step implementation PLAN to resolve this ticket.",
        "You have read-only access — explore the repo, then OUTPUT THE COMPLETE",
        "PLAN AS YOUR FINAL MESSAGE (do not attempt to edit files or use any",
        "plan-approval tool). Identify the files to change, the approach, and how",
        "to verify. Be concise but complete.",
    ]
    if revise_notes:
        p += ["", "The reviewer requested changes to your previous plan:",
              revise_notes, "Revise the plan accordingly."]
    return "\n".join(x for x in p if x is not None)


def implement_prompt(ticket: dict, repo: Path, plan: str | None,
                     reviewer_notes: str | None = None) -> str:
    p = [
        f"You are resolving Stingray ticket #{ticket['id']}.",
        f"Your working directory is a dedicated checkout at {repo} — work there and",
        "use paths relative to it. Make the code changes and run the project's tests",
        "if present. Do NOT commit or push — just leave the changes in the working tree.",
        "",
        "Constraints for this automated run (no human is watching the terminal,",
        "and the run is killed at a hard time limit):",
        "- Do NOT start long-running or foreground processes: no dev/app servers",
        "  (`uvicorn`, `npm run dev`, `flask run`, `vite`), no `--watch` modes, no",
        "  `docker-compose up` without `-d`, and nothing interactive. They never",
        "  return and will consume the entire time budget.",
        "- Run only the project's automated, non-interactive test suite, and wrap",
        "  any command that might block in a shell `timeout` (e.g.",
        "  `timeout 180 pytest -q`).",
        "- If the approved plan's verification asks for a live/booted server or",
        "  manual behavioral testing, treat that as OUT OF SCOPE for this run: rely",
        "  on the automated tests and static import checks (e.g.",
        "  `python -c 'import module'`) to validate the change instead.",
        "",
        "To file a SEPARATE Stingray ticket during this run (a review request for the",
        "changes you made, or a follow-up issue you noticed) do NOT hand-write curl —",
        "run the resolver's validated filer from the repo root:",
        f"  {sys.executable} {HERE / 'file_ticket.py'} \\",
        "    --type code_review|task --title \"...\" [--priority low|medium|high|critical] \\",
        "    [--tag NAME ...] [--code-block PATH:LANGUAGE:START-END ...]",
        "It reads the Stingray URL and API key from the resolver config (you do not",
        "supply them), and --code-block reads the exact lines off disk so you never",
        "escape code by hand. Only file one if the ticket asks for it or it's clearly",
        "warranted; otherwise skip it.",
        "",
    ]
    if plan:
        p += ["Implement this APPROVED plan:", "", plan, ""]
    if reviewer_notes:
        p += [
            "A reviewer looked at your existing PR branch and requested these "
            "changes — address them on top of the work already on the branch:",
            "",
            reviewer_notes,
            "",
        ]
    p += [
        "Original ticket:",
        f"Title: {ticket['title']}",
        "Description:",
        ticket.get("description") or "(none)",
        render_code_blocks(ticket),
        "",
        "When done, output a short summary of what you changed and the test results.",
    ]
    return "\n".join(x for x in p if x is not None)


# --- phase handlers ------------------------------------------------------
def do_plan(cfg: Config, client: StingrayClient, ticket: dict, repo: Path,
            revise_notes: str | None) -> None:
    # Ack first, then claim: a transient failure posting the ack shouldn't leave
    # the ticket claimed (claude:planning) but silent.
    agent_label = agents.get_runner(cfg.agent).label
    client.add_comment(ticket["id"], f"🔧 {agent_label} is " +
        ("revising the plan" if revise_notes else "planning this ticket") +
        " — read-only, this can take a few minutes. I'll post the plan and "
        "reassign it back to you when done.")
    set_state(client, ticket, [TAG_PLANNING])
    phase("planning", ticket, f"#{ticket['id']}: planning ({'revise' if revise_notes else 'fresh'})")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = cfg.logs_dir / f"ticket-{ticket['id']}-plan-{ts}.log"
    ok, result = run_agent(cfg, plan_prompt(ticket, repo, revise_notes), repo, "plan", log_path)
    if not ok:
        fail(client, ticket, f"Planning failed.\n\n```\n{tail(result)}\n```")
        return
    body = (
        f"{PLAN_MARKER} (Stingray resolver)\n\n{result}\n\n---\n"
        "Reply with `/approve` (and re-assign this ticket to me) to implement, "
        "or `/revise <notes>` to adjust the plan."
    )
    client.add_comment(ticket["id"], body)
    set_state(client, ticket, [TAG_AWAIT_PLAN], status="in_review",
              assigned_to=ticket["created_by"])
    phase("awaiting-plan-approval", ticket,
          f"#{ticket['id']}: posted plan, handed back to user {ticket['created_by']}")


_FILED_RE = re.compile(r"created ticket #(\d+)")


def filed_tickets_in_log(log_path: Path) -> list[int]:
    """Ticket ids that file_ticket.py reported creating during a run — it prints
    `created ticket #<id>`, which lands in the agent's captured tool output. Lets
    an implement run that filed tickets but changed no code be reported as a
    success instead of a misleading 'produced no code changes'."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    seen: list[int] = []
    for m in _FILED_RE.finditer(text):
        tid = int(m.group(1))
        if tid not in seen:
            seen.append(tid)
    return seen


def do_implement(cfg: Config, client: StingrayClient, ticket: dict, repo: Path,
                 plan: str | None, reviewer_notes: str | None = None) -> None:
    set_state(client, ticket, [TAG_IMPLEMENTING])
    agent_label = agents.get_runner(cfg.agent).label
    client.add_comment(ticket["id"], f"🔧 {agent_label} is implementing this — working on a "
        "branch, this can take a few minutes. I'll post a summary and reassign it "
        "back to you when done.")
    phase("implementing", ticket, f"#{ticket['id']}: implementing"
          + (" (rework)" if reviewer_notes else ""))
    # Compute remote/PR availability once and pass it down (cheaper, consistent).
    origin = has_origin(repo)
    pr_ok = origin and run(["gh", "auth", "status"])[0] == 0
    if origin:
        run(["git", "-C", str(repo), "fetch", "origin"], timeout=cfg.git_net_timeout)
    base_ref, base_branch = resolve_base(repo)
    wt, branch = prepare_worktree(repo, ticket["id"], base_ref)
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        log_path = cfg.logs_dir / f"ticket-{ticket['id']}-implement-{ts}.log"
        ok, summary = run_agent(cfg, implement_prompt(ticket, wt, plan, reviewer_notes),
                                wt, "implement", log_path)
        if not ok:
            # An approved plan exists — hand back re-implementable so a timeout
            # doesn't throw the plan away and re-plan from scratch on retry.
            fail(client, ticket, f"Implementation failed.\n\n```\n{tail(summary)}\n```",
                 reimplementable=True)
            return

        run(["git", "-C", str(wt), "add", "-A"])
        # Set the committer identity explicitly: on a host with no global git
        # identity the commit otherwise fails, and the empty tree downstream gets
        # misreported as the agent having "produced no code changes".
        run(["git", "-C", str(wt),
             "-c", f"user.name={cfg.git_author_name}",
             "-c", f"user.email={cfg.git_author_email}",
             "commit", "-m", f"Resolve Stingray #{ticket['id']}: {ticket['title']}"])
        ahead = run(["git", "-C", str(wt), "rev-list", "--count", f"{base_ref}..HEAD"])[1].strip()
        if ahead in ("", "0"):
            # No diff — but if the run's whole job was to file Stingray ticket(s)
            # via file_ticket.py, that's a success, not a failure. Detect the
            # tickets it filed and hand this one back as done instead of looking
            # like the agent did nothing.
            filed = filed_tickets_in_log(log_path)
            if filed:
                ids = ", ".join(f"#{i}" for i in filed)
                client.add_comment(ticket["id"],
                    f"{IMPL_MARKER} — no code changes were needed; filed {ids}.\n\n{summary}")
                set_state(client, ticket, [], status="in_review",
                          assigned_to=ticket["created_by"])
                phase("filed-no-code", ticket,
                      f"#{ticket['id']}: filed {ids}, no code changes — handed back")
                return
            fail(client, ticket, f"{agent_label} produced no code changes for this ticket.",
                 reimplementable=True)
            return

        stat = run(["git", "-C", str(wt), "diff", "--stat", f"{base_ref}..HEAD"])[1].strip()
        publish(cfg, client, ticket, repo, wt, branch, base_ref, base_branch,
                summary, stat, origin=origin, pr_ok=pr_ok)
    finally:
        remove_worktree(repo, wt)


def publish(cfg, client, ticket, repo, wt, branch, base_ref, base_branch, summary, stat,
            *, origin: bool, pr_ok: bool) -> None:
    """Open a PR (or fall back to branch/patch) and hand the ticket back.

    Every repo-writing command's exit code is checked: a failed `git push` must
    NOT proceed to PR creation and post a misleading "Implemented" comment with
    no link — the branch never reached the remote, so we hand the ticket back
    re-implementable with the push error instead."""
    tid = ticket["id"]
    if cfg.patch_fallback:
        diff = run(["git", "-C", str(wt), "diff", f"{base_ref}..HEAD"])[1]
        body = f"{IMPL_MARKER} (patch — apply manually)\n\n{summary}\n\n```diff\n{tail(diff, 12000)}\n```"
        run(["git", "-C", str(repo), "branch", "-D", branch])  # discard, nothing persisted
    elif pr_ok:
        rc, push_out = run(["git", "-C", str(wt), "push", "--force-with-lease", "-u",
                            "origin", branch], timeout=cfg.git_net_timeout)
        if rc != 0:
            fail(client, ticket,
                 f"Pushed nothing — `git push` failed, so no PR was opened and the "
                 f"work is on the local branch only.\n\n```\n{tail(push_out)}\n```",
                 reimplementable=True)
            return
        pr_body = f"{summary}\n\nResolves Stingray #{tid}."
        rc, out = run(["gh", "pr", "create", "--title", f"Resolve #{tid}: {ticket['title']}",
                       "--body", pr_body, "--head", branch, "--base", base_branch],
                      cwd=wt, timeout=cfg.git_net_timeout)
        if rc == 0:
            url = out.strip().splitlines()[-1] if out.strip() else ""
        else:
            # `gh pr create` fails when a PR already exists for the branch — in
            # that case `gh pr view` gives us the existing URL. Any other failure
            # leaves url blank and we fall back to the local-branch message.
            url = run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
                      cwd=wt)[1].strip()
        if url:
            body = f"{IMPL_MARKER} — {url}\n\n{summary}\n\nChanged files:\n```\n{stat}\n```"
        else:
            body = (f"{IMPL_MARKER} and pushed to branch `{branch}`, but opening a PR "
                    f"failed.\n\n{summary}\n\nChanged files:\n```\n{stat}\n```")
    else:
        reason = ("`gh` is not authenticated — run `gh auth login` to get PRs"
                  if origin else "no GitHub remote configured")
        body = (f"{IMPL_MARKER} on local branch `{branch}` ({reason}).\n\n"
                f"{summary}\n\nChanged files:\n```\n{stat}\n```")

    client.add_comment(tid, body)
    set_state(client, ticket, [TAG_AWAIT_PR], status="in_review", assigned_to=ticket["created_by"])
    phase("awaiting-pr-review", ticket,
          f"#{tid}: implemented, handed back to user {ticket['created_by']}")


def fail(client: StingrayClient, ticket: dict, message: str, *,
         reimplementable: bool = False) -> None:
    """Report a failure and notify the reporter.

    By default the ticket is dropped back to `open` with no claim tags. When
    `reimplementable=True` (an *implement*-phase failure where an approved plan
    already exists), the ticket is instead handed back awaiting plan approval,
    so a fresh `/approve` re-enters the implement phase with the existing plan
    rather than discarding it and re-planning from scratch."""
    if reimplementable:
        message += ("\n\n_Re-assign to me with `/approve` to retry the implement "
                    "step using the existing approved plan._")
    # Best-effort: even if posting the comment throws (despite client retries),
    # still reset the ticket's state so a wobble can't strand it mid-claim.
    try:
        client.add_comment(ticket["id"], f"{FAIL_MARKER} this ticket.\n\n{message}")
    except Exception as e:
        audit.get_logger().warning("#%s: fail() could not post comment: %r", ticket["id"], e)
    if reimplementable:
        set_state(client, ticket, [TAG_AWAIT_PLAN], status="in_review",
                  assigned_to=ticket["created_by"])
    else:
        set_state(client, ticket, [], status="open", assigned_to=ticket["created_by"])
    phase("failed", ticket, f"#{ticket['id']}: FAILED — {message.splitlines()[0]}",
          reimplementable=reimplementable)


def tail(text: str, limit: int = 3000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]


# --- dispatch ------------------------------------------------------------
def process(cfg: Config, client: StingrayClient, ticket: dict, dry_run: bool) -> None:
    tid = ticket["id"]
    audit.set_ticket(tid)
    tags = claude_tags(ticket)
    dangerous = TAG_DANGEROUS in ticket.get("tags", [])
    status = ticket.get("status")

    # Resolve & sandbox-check the target repo up front.
    try:
        repo = cfg.resolve_repo(repo_name_of(ticket))
    except (RepoNotAllowed, RepoNotFound) as e:
        if dry_run:
            log(f"#{tid}: would REJECT — {e}")
        else:
            fail(client, ticket, f"Cannot resolve target repo: {e}")
        return

    # Fetch comments once and derive everything from the single list (B7).
    comments = client.list_comments(tid)

    # Honor any `/ticket` directives in the body/comments before the normal
    # plan/implement dispatch — filing a follow-up ticket is independent of this
    # ticket's own workflow state.
    handle_ticket_directives(cfg, client, ticket, comments, repo, dry_run)

    # A ticket whose body is *only* a `/ticket` directive is a pure filing
    # request — there's nothing to plan or implement. File it (above), then hand
    # it back to the author instead of spending an agent run planning it.
    if body_is_directive_only(ticket):
        if dry_run:
            log(f"#{tid}: directive-only ticket — would file and hand back (no plan)")
            return
        author = ticket.get("created_by")
        if author and author != cfg.bot_user_id:
            set_state(client, ticket, [], status="in_review", assigned_to=author)
        log(f"#{tid}: directive-only ticket — filed, handed back to {author} (no plan)")
        return

    last = latest_human(comments, cfg.bot_user_id)
    cmd = (last.get("body") or "").strip().lower() if last else ""

    if TAG_AWAIT_PLAN in tags:
        if cmd.startswith("/approve"):
            plan = find_approved_plan(comments, cfg.bot_user_id)
            action, kw = "implement", {"plan": plan}
        elif cmd.startswith("/revise") or status == "changes_requested":
            notes = (last["body"].split(None, 1)[1] if last and len(last["body"].split(None, 1)) > 1 else "")
            action, kw = "replan", {"revise_notes": notes}
        else:
            action, kw = "nudge", {}
    elif TAG_AWAIT_PR in tags:
        if status == "changes_requested":
            # Thread the reviewer's change request into the rework so Claude
            # isn't re-implementing blind (B4).
            action, kw = "rework", {"plan": find_approved_plan(comments, cfg.bot_user_id),
                                    "reviewer_notes": last["body"] if last else None}
        else:
            action, kw = "skip", {}
    elif TAG_PLANNING in tags:
        action, kw = "replan", {"revise_notes": None}   # retry after a crashed plan run
    elif TAG_IMPLEMENTING in tags or dangerous:
        action, kw = "implement", {"plan": find_approved_plan(comments, cfg.bot_user_id)}
    else:
        action, kw = "plan", {"revise_notes": None}

    log(f"#{tid}: action={action} repo={repo.name} dangerous={dangerous} status={status}")
    if dry_run:
        return

    # Attempt cap (B3): a ticket that keeps failing the same phase shouldn't be
    # auto-retried forever, burning tokens on every cron tick. Once the streak of
    # recent failures hits the cap, hand it to a human and leave the bot's queue.
    if cfg.max_attempts > 0 and action in ("plan", "replan", "implement", "rework"):
        failures = recent_failures(comments, cfg.bot_user_id)
        if failures >= cfg.max_attempts:
            give_up(client, ticket, failures)
            return

    if action == "plan":
        do_plan(cfg, client, ticket, repo, kw["revise_notes"])
    elif action == "replan":
        do_plan(cfg, client, ticket, repo, kw["revise_notes"])
    elif action in ("implement", "rework"):
        do_implement(cfg, client, ticket, repo, kw.get("plan"), kw.get("reviewer_notes"))
    elif action == "nudge":
        client.add_comment(tid, "I need an explicit `/approve` or `/revise <notes>` comment "
                                "to proceed. Re-assign to me with one of those.")
        set_state(client, ticket, [TAG_AWAIT_PLAN], status="in_review",
                  assigned_to=ticket["created_by"])
    # "skip": nothing to do.


def give_up(client: StingrayClient, ticket: dict, failures: int) -> None:
    """Stop auto-retrying a repeatedly-failing ticket: notify, drop claim tags,
    reopen, and hand it to the reporter so it leaves the bot's sweep."""
    client.add_comment(
        ticket["id"],
        f"🛑 Giving up after {failures} failed attempt(s). I've reopened this "
        "ticket and handed it back — a human will need to take a look. Re-assign "
        "it to me to make me try again.")
    set_state(client, ticket, [], status="open", assigned_to=ticket["created_by"])
    phase("gave-up", ticket, f"#{ticket['id']}: gave up after {failures} attempts",
          failures=failures)


def sweep(cfg: Config, client: StingrayClient, dry_run: bool, only: int | None,
          max_tickets: int = 0) -> int:
    """Process the bot's queue; return how many tickets were processed (used by
    main() to drop the log files of an empty sweep)."""
    if only is not None:
        process(cfg, client, client.get_ticket(only), dry_run)
        return 1
    # Anything currently assigned to the bot is ours to act on, regardless of
    # status (after /approve the human reassigns but leaves status=in_review).
    # Terminal statuses are skipped so we never re-plan a finished ticket.
    # Oldest-first (by id) so a bounded sweep drains the backlog fairly; the next
    # cron tick continues where this one stopped.
    tickets = [t for t in client.iter_tickets(assigned_to=cfg.bot_user_id)
               if t.get("status") not in ("resolved", "closed")]
    tickets.sort(key=lambda t: t.get("id", 0))
    processed = 0
    for ticket in tickets:
        if max_tickets and processed >= max_tickets:
            log(f"reached max-tickets={max_tickets}; {len(tickets) - processed} left for next sweep")
            break
        processed += 1
        try:
            process(cfg, client, ticket, dry_run)
        except Exception as e:  # one bad ticket shouldn't kill the sweep
            audit.set_ticket(ticket.get("id"))
            log(f"#{ticket['id']}: ERROR {e!r}")
            if not dry_run:
                try:
                    fail(client, ticket, f"Resolver error: {e!r}")
                except Exception:
                    pass
        finally:
            audit.set_ticket(None)
    return processed


def main() -> None:
    ap = argparse.ArgumentParser(description="Stingray ticket resolver sweep")
    ap.add_argument("--ticket", type=int, help="process only this ticket id")
    ap.add_argument("--dry-run", action="store_true", help="report actions without acting")
    ap.add_argument("--max-tickets", type=int, default=None,
                    help="process at most N tickets this sweep (default: MAX_TICKETS_PER_SWEEP)")
    args = ap.parse_args()

    cfg = Config.load()
    global AUDIT_TAIL_BYTES
    AUDIT_TAIL_BYTES = cfg.audit_output_tail_bytes
    sweep_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    logger = audit.setup_logging(cfg, sweep_id)
    audit.maintain_logs(cfg, logger)

    # Fail fast on an unknown RESOLVER_AGENT before touching the network.
    runner = agents.get_runner(cfg.agent)
    max_tickets = args.max_tickets if args.max_tickets is not None else cfg.max_tickets_per_sweep
    client = StingrayClient(cfg.stingray_url, cfg.api_key,
                            max_retries=cfg.stingray_max_retries, logger=logger)
    log(f"sweep start (agent {runner.name}, bot user {cfg.bot_user_id}, "
        f"root {cfg.projects_root}, max_tickets={max_tickets or 'unlimited'})")
    processed = sweep(cfg, client, args.dry_run, args.ticket, max_tickets)
    log("sweep done")
    # An empty sweep did nothing worth a trace — drop its own log/audit pair so
    # ~288 idle sweeps/day don't bury the dir in near-empty files. A sweep that
    # errored propagates past here, keeping its log.
    if not processed and not args.dry_run:
        audit.discard_sweep_logs(cfg, sweep_id)


if __name__ == "__main__":
    sys.exit(main())
