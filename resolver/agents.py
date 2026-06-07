"""Agent-runner abstraction.

The resolver's orchestration (sweep → plan → implement → PR) is agent-agnostic;
only the *invocation* of the coding agent is tool-specific. This module defines
the seam so a single resolver codebase can drive different agents (Claude Code
today, an OpenAI Codex CLI tomorrow) selected per resolver identity via the
``RESOLVER_AGENT`` env var.

An ``AgentRunner`` implements one method, ``run(cfg, prompt, cwd, mode,
log_path) -> (ok, result_text)``, where ``mode`` is ``"plan"`` (read-only
exploration; return the plan text) or ``"implement"`` (edits allowed in ``cwd``;
return a summary). The Claude implementation lives in ``resolve_tickets.py``
(``ClaudeRunner``) next to the stream-json parsing it reuses, and registers
itself on import.

To add a second agent (e.g. Codex), subclass ``AgentRunner``, implement ``run``
to drive that CLI, and ``register_runner(YourRunner())`` — see ``CodexRunner``
below for the template. Point a resolver at it with ``RESOLVER_AGENT=<name>``
and give that resolver its own bot user id (``RESOLVER_BOT_USER_ID``) so the two
resolvers sweep disjoint ticket queues.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Config


class AgentRunner(ABC):
    """Drives a headless coding agent for one plan or implement phase."""

    #: stable key matched against RESOLVER_AGENT (e.g. "claude", "codex").
    name: str = ""
    #: human-friendly label used in user-facing ticket comments/logs.
    label: str = ""

    @abstractmethod
    def run(self, cfg: "Config", prompt: str, cwd: Path, mode: str,
            log_path: Path) -> tuple[bool, str]:
        """Run one phase. ``mode`` is 'plan' (read-only; return the plan) or
        'implement' (edits allowed in ``cwd``; return a summary). Tee raw output
        to ``log_path``. Return ``(ok, result_text)``."""
        raise NotImplementedError


_REGISTRY: dict[str, AgentRunner] = {}


def register_runner(runner: AgentRunner) -> None:
    """Register an agent runner under its ``name`` (last registration wins)."""
    if not runner.name:
        raise ValueError("agent runner must set a non-empty `name`")
    _REGISTRY[runner.name] = runner


def get_runner(name: str) -> AgentRunner:
    """Return the registered runner for ``name`` or exit with a clear message."""
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise SystemExit(
            f"resolver: unknown RESOLVER_AGENT={name!r}; registered agents: {available}. "
            "Add one by subclassing agents.AgentRunner and calling "
            "agents.register_runner(); see CodexRunner in agents.py for a template."
        )


class CodexRunner(AgentRunner):
    """Template for an OpenAI Codex-CLI resolver. **Not yet implemented.**

    To enable a Codex resolver:
      1. Implement ``run`` to invoke the ``codex`` CLI for the given ``mode``
         (read-only tools for "plan", edits for "implement"), tee its output to
         ``log_path``, and return ``(ok, result_text)`` — mirror the structure
         of ``ClaudeRunner.run`` / ``run_claude`` in resolve_tickets.py.
      2. Append ``register_runner(CodexRunner())`` at the bottom of this module.
      3. Run a resolver with ``RESOLVER_AGENT=codex`` and its own
         ``RESOLVER_BOT_USER_ID`` (a separate ``codex-bot`` user).
    """

    name = "codex"
    label = "Codex"

    def run(self, cfg: "Config", prompt: str, cwd: Path, mode: str,
            log_path: Path) -> tuple[bool, str]:
        raise NotImplementedError(
            "the codex agent-runner is a template, not yet implemented; "
            "see CodexRunner in agents.py"
        )


# CodexRunner is intentionally left UNregistered until implemented, so selecting
# RESOLVER_AGENT=codex fails fast at startup with actionable guidance rather than
# stranding a ticket mid-sweep. Uncomment to enable once `run` is written:
# register_runner(CodexRunner())
