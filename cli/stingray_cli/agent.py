"""A minimal local-agent wrapper: prompt in, text out.

Deliberately *not* the resolver's ``agents.AgentRunner``. That ABC takes a
resolver ``Config`` and a phase, and its implementations are wired to the
resolver's audit logging, worktree lifecycle and token accounting. A pipx-installed
CLI that just wants one paragraph of prose should not drag any of that in.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# Preference order when nothing is configured.
KNOWN_AGENTS = ("claude", "opencode")


class AgentError(Exception):
    """The agent was unavailable, failed, or produced nothing usable."""


def detect(preferred: str | None = None) -> str | None:
    """The first usable agent binary, or None if the machine has none."""
    candidates = [preferred] if preferred else list(KNOWN_AGENTS)
    for name in candidates:
        if name and shutil.which(name):
            return name
    return None


def run(prompt: str, cwd: Path, *, agent: str | None = None,
        model: str | None = None, timeout: int = 180,
        edit: bool = False) -> str:
    """Run ``prompt`` through a local agent and return its final text.

    ``edit=True`` lets the agent modify files in ``cwd`` (used by scaffold);
    otherwise the run is read-only.
    """
    name = detect(agent)
    if not name:
        want = agent or " or ".join(KNOWN_AGENTS)
        raise AgentError(f"no local agent found on PATH (looked for {want})")

    if name == "claude":
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        if model:
            cmd += ["--model", model]
        cmd += (["--permission-mode", "acceptEdits"] if edit
                else ["--permission-mode", "default", "--allowedTools", "Read", "Glob", "Grep"])
    elif name == "opencode":
        # --dir is required: without it opencode roots itself in the global
        # project rather than the directory we care about.
        cmd = ["opencode", "run", prompt, "--format", "json", "--dir", str(cwd)]
        if model:
            cmd += ["--model", model]
        cmd += ["--agent", "build" if edit else "plan"]
    else:
        raise AgentError(f"unsupported agent: {name}")

    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AgentError(
            f"{name} timed out after {timeout}s — raise it with "
            f"`timeout = <seconds>` under [profile.<name>.describe] in your config"
        ) from exc
    except OSError as exc:
        raise AgentError(f"could not run {name}: {exc}") from exc

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise AgentError(f"{name} exited {proc.returncode}: {' / '.join(tail)}")

    return _extract_text(proc.stdout)


def _extract_text(stdout: str) -> str:
    """Pull the final assistant text out of an agent's JSON envelope.

    Falls back to raw stdout: the envelope shape varies between agents and
    versions, and raw text is still parseable by the caller.
    """
    stdout = stdout.strip()
    if not stdout:
        raise AgentError("agent produced no output")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(data, dict):
        for key in ("result", "text", "output", "content", "response"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return stdout
