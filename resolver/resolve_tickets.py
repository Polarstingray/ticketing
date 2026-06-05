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
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import Config, RepoNotAllowed, RepoNotFound
from stingray import StingrayClient

# --- tag conventions -----------------------------------------------------
CLAUDE_PREFIX = "claude:"
TAG_PLANNING = "claude:planning"            # plan run in flight
TAG_AWAIT_PLAN = "claude:awaiting-plan-approval"
TAG_IMPLEMENTING = "claude:implementing"    # implement run in flight
TAG_AWAIT_PR = "claude:awaiting-pr-review"
TAG_DANGEROUS = "dangerous"
REPO_TAG_PREFIX = "repo:"

PLAN_MARKER = "📋 **Proposed plan**"
WORK_DIR = Path(__file__).resolve().parent / "work"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd: list[str], cwd: str | Path | None = None, timeout: int | None = 120):
    """Run a command, capturing combined output. Returns (rc, output)."""
    proc = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return proc.returncode, proc.stdout


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


def find_approved_plan(client: StingrayClient, ticket_id: int, bot_id: int) -> str | None:
    """The most recent bot comment that carries the plan marker."""
    for c in reversed(client.list_comments(ticket_id)):
        if c.get("author") == bot_id and PLAN_MARKER in (c.get("body") or ""):
            return c["body"]
    return None


# --- Claude runner -------------------------------------------------------
def run_claude(cfg: Config, prompt: str, cwd: Path, mode: str, log_path: Path) -> tuple[bool, str]:
    """Run headless Claude. mode is 'plan' (read-only) or 'implement'.
    Returns (ok, result_text)."""
    cmd = [cfg.claude_bin, "-p", prompt, "--output-format", "json"]
    if cfg.claude_model:
        cmd += ["--model", cfg.claude_model]
    if mode == "plan":
        # Read-only exploration. We deliberately do NOT use --permission-mode
        # plan: headless, that routes the plan through ExitPlanMode (which can't
        # be approved non-interactively), so the plan text never reaches the
        # JSON `result`. Granting only read tools and asking for the plan as the
        # final message captures it cleanly while guaranteeing no edits.
        cmd += ["--permission-mode", "default", "--allowedTools", "Read", "Glob", "Grep"]
    else:
        cmd += ["--permission-mode", "acceptEdits"]
        if cfg.implement_tools:
            cmd += ["--allowedTools", *cfg.implement_tools.split()]

    try:
        rc, out = run(cmd, cwd=cwd, timeout=cfg.claude_timeout)
    except subprocess.TimeoutExpired:
        log_path.write_text(f"TIMEOUT after {cfg.claude_timeout}s\n")
        return False, f"Claude timed out after {cfg.claude_timeout}s."

    log_path.write_text(out)
    # --output-format json prints a single JSON object with a `result` field.
    result_text = out.strip()
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            result_text = data.get("result") or result_text
            if data.get("is_error") or data.get("subtype") not in (None, "success"):
                return False, result_text
    except json.JSONDecodeError:
        if rc != 0:
            return False, result_text or f"claude exited {rc}"
    return rc == 0, result_text


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


def pr_available(repo: Path) -> bool:
    return has_origin(repo) and run(["gh", "auth", "status"])[0] == 0


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


def implement_prompt(ticket: dict, repo: Path, plan: str | None) -> str:
    p = [
        f"You are resolving Stingray ticket #{ticket['id']}.",
        f"Your working directory is a dedicated checkout at {repo} — work there and",
        "use paths relative to it. Make the code changes and run the project's tests",
        "if present. Do NOT commit or push — just leave the changes in the working tree.",
        "",
    ]
    if plan:
        p += ["Implement this APPROVED plan:", "", plan, ""]
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
    set_state(client, ticket, [TAG_PLANNING])
    client.add_comment(ticket["id"], "🔧 Claude is " +
        ("revising the plan" if revise_notes else "planning this ticket") +
        " — read-only, this can take a few minutes. I'll post the plan and "
        "reassign it back to you when done.")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = cfg.logs_dir / f"ticket-{ticket['id']}-plan-{ts}.log"
    ok, result = run_claude(cfg, plan_prompt(ticket, repo, revise_notes), repo, "plan", log_path)
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
    log(f"#{ticket['id']}: posted plan, handed back to user {ticket['created_by']}")


def do_implement(cfg: Config, client: StingrayClient, ticket: dict, repo: Path,
                 plan: str | None) -> None:
    set_state(client, ticket, [TAG_IMPLEMENTING])
    client.add_comment(ticket["id"], "🔧 Claude is implementing this — working on a "
        "branch, this can take a few minutes. I'll post a summary and reassign it "
        "back to you when done.")
    if has_origin(repo):
        run(["git", "-C", str(repo), "fetch", "origin"])
    base_ref, base_branch = resolve_base(repo)
    wt, branch = prepare_worktree(repo, ticket["id"], base_ref)
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        log_path = cfg.logs_dir / f"ticket-{ticket['id']}-implement-{ts}.log"
        ok, summary = run_claude(cfg, implement_prompt(ticket, wt, plan), wt, "implement", log_path)
        if not ok:
            fail(client, ticket, f"Implementation failed.\n\n```\n{tail(summary)}\n```")
            return

        run(["git", "-C", str(wt), "add", "-A"])
        run(["git", "-C", str(wt), "commit", "-m",
             f"Resolve Stingray #{ticket['id']}: {ticket['title']}"])
        ahead = run(["git", "-C", str(wt), "rev-list", "--count", f"{base_ref}..HEAD"])[1].strip()
        if ahead in ("", "0"):
            fail(client, ticket, "Claude produced no code changes for this ticket.")
            return

        stat = run(["git", "-C", str(wt), "diff", "--stat", f"{base_ref}..HEAD"])[1].strip()
        publish(cfg, client, ticket, repo, wt, branch, base_ref, base_branch, summary, stat)
    finally:
        remove_worktree(repo, wt)


def publish(cfg, client, ticket, repo, wt, branch, base_ref, base_branch, summary, stat) -> None:
    """Open a PR (or fall back to branch/patch) and hand the ticket back."""
    tid = ticket["id"]
    if cfg.patch_fallback:
        diff = run(["git", "-C", str(wt), "diff", f"{base_ref}..HEAD"])[1]
        body = f"✅ **Implemented** (patch — apply manually)\n\n{summary}\n\n```diff\n{tail(diff, 12000)}\n```"
        run(["git", "-C", str(repo), "branch", "-D", branch])  # discard, nothing persisted
    elif pr_available(repo):
        run(["git", "-C", str(wt), "push", "--force-with-lease", "-u", "origin", branch])
        pr_body = f"{summary}\n\nResolves Stingray #{tid}."
        rc, out = run(["gh", "pr", "create", "--title", f"Resolve #{tid}: {ticket['title']}",
                       "--body", pr_body, "--head", branch, "--base", base_branch], cwd=wt)
        url = out.strip().splitlines()[-1] if rc == 0 else \
            run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"], cwd=wt)[1].strip()
        body = f"✅ **Implemented** — {url}\n\n{summary}\n\nChanged files:\n```\n{stat}\n```"
    else:
        reason = ("`gh` is not authenticated — run `gh auth login` to get PRs"
                  if has_origin(repo) else "no GitHub remote configured")
        body = (f"✅ **Implemented** on local branch `{branch}` ({reason}).\n\n"
                f"{summary}\n\nChanged files:\n```\n{stat}\n```")

    client.add_comment(tid, body)
    set_state(client, ticket, [TAG_AWAIT_PR], status="in_review", assigned_to=ticket["created_by"])
    log(f"#{tid}: implemented, handed back to user {ticket['created_by']}")


def fail(client: StingrayClient, ticket: dict, message: str) -> None:
    """Report a failure, drop claim tags, leave open, and notify the reporter."""
    client.add_comment(ticket["id"], f"⚠️ Resolver could not complete this ticket.\n\n{message}")
    set_state(client, ticket, [], status="open", assigned_to=ticket["created_by"])
    log(f"#{ticket['id']}: FAILED — {message.splitlines()[0]}")


def tail(text: str, limit: int = 3000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]


# --- dispatch ------------------------------------------------------------
def process(cfg: Config, client: StingrayClient, ticket: dict, dry_run: bool) -> None:
    tid = ticket["id"]
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

    # Decide the action from the current sub-state.
    last = client.latest_human_comment(tid, cfg.bot_user_id)
    cmd = (last.get("body") or "").strip().lower() if last else ""

    if TAG_AWAIT_PLAN in tags:
        if cmd.startswith("/approve"):
            plan = find_approved_plan(client, tid, cfg.bot_user_id)
            action, kw = "implement", {"plan": plan}
        elif cmd.startswith("/revise") or status == "changes_requested":
            notes = (last["body"].split(None, 1)[1] if last and len(last["body"].split(None, 1)) > 1 else "")
            action, kw = "replan", {"revise_notes": notes}
        else:
            action, kw = "nudge", {}
    elif TAG_AWAIT_PR in tags:
        action, kw = ("rework", {}) if status == "changes_requested" else ("skip", {})
    elif TAG_PLANNING in tags:
        action, kw = "replan", {"revise_notes": None}   # retry after a crashed plan run
    elif TAG_IMPLEMENTING in tags or dangerous:
        action, kw = "implement", {"plan": find_approved_plan(client, tid, cfg.bot_user_id)}
    else:
        action, kw = "plan", {"revise_notes": None}

    log(f"#{tid}: action={action} repo={repo.name} dangerous={dangerous} status={status}")
    if dry_run:
        return

    if action == "plan":
        do_plan(cfg, client, ticket, repo, kw["revise_notes"])
    elif action == "replan":
        do_plan(cfg, client, ticket, repo, kw["revise_notes"])
    elif action in ("implement", "rework"):
        do_implement(cfg, client, ticket, repo, kw.get("plan"))
    elif action == "nudge":
        client.add_comment(tid, "I need an explicit `/approve` or `/revise <notes>` comment "
                                "to proceed. Re-assign to me with one of those.")
        set_state(client, ticket, [TAG_AWAIT_PLAN], status="in_review",
                  assigned_to=ticket["created_by"])
    # "skip": nothing to do.


def sweep(cfg: Config, client: StingrayClient, dry_run: bool, only: int | None) -> None:
    if only is not None:
        process(cfg, client, client.get_ticket(only), dry_run)
        return
    # Anything currently assigned to the bot is ours to act on, regardless of
    # status (after /approve the human reassigns but leaves status=in_review).
    # Terminal statuses are skipped so we never re-plan a finished ticket.
    for ticket in client.iter_tickets(assigned_to=cfg.bot_user_id):
        if ticket.get("status") in ("resolved", "closed"):
            continue
        try:
            process(cfg, client, ticket, dry_run)
        except Exception as e:  # one bad ticket shouldn't kill the sweep
            log(f"#{ticket['id']}: ERROR {e!r}")
            if not dry_run:
                try:
                    fail(client, ticket, f"Resolver error: {e!r}")
                except Exception:
                    pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Stingray ticket resolver sweep")
    ap.add_argument("--ticket", type=int, help="process only this ticket id")
    ap.add_argument("--dry-run", action="store_true", help="report actions without acting")
    args = ap.parse_args()

    cfg = Config.load()
    client = StingrayClient(cfg.stingray_url, cfg.api_key)
    log(f"sweep start (bot user {cfg.bot_user_id}, root {cfg.projects_root})")
    sweep(cfg, client, args.dry_run, args.ticket)
    log("sweep done")


if __name__ == "__main__":
    sys.exit(main())
