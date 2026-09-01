"""Sliding-context summarization for chat history.

When the total character count of replayed history exceeds a threshold, the
oldest messages are condensed into a single synthetic exchange that is
prepended to the recent verbatim turns. This keeps the context window
meaningful even in long threads without simply dropping older messages.

Summarization is a best-effort path: a provider failure returns the original
history unchanged so the turn degrades gracefully rather than failing.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .provider import ProviderError, complete

if TYPE_CHECKING:
    from .config import ChatConfig

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM = (
    "Summarize this conversation concisely in 3–5 sentences, "
    "preserving key facts and decisions."
)


def _format_for_summary(history: list[tuple[str, str]]) -> str:
    lines = []
    for role, content in history:
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {content}")
    return "\n\n".join(lines)


def maybe_summarize(
    history: list[tuple[str, str]],
    cfg: "ChatConfig",
    *,
    threshold: int,
    keep_turns: int,
) -> list[tuple[str, str]]:
    """Return history, condensing old messages when total chars exceed threshold.

    Returns ``history`` unchanged when:
    - total chars ≤ threshold, or
    - there are not enough messages to have anything outside keep_turns.

    On provider failure the original history is returned unchanged.
    """
    total_chars = sum(len(content) for _, content in history)
    min_messages = keep_turns * 2

    if total_chars <= threshold or len(history) <= min_messages:
        return history

    split = len(history) - min_messages
    to_summarize = history[:split]
    to_keep = history[split:]

    try:
        completion = complete(cfg, _SUMMARY_SYSTEM, _format_for_summary(to_summarize))
        summary_text = completion.text
    except ProviderError:
        logger.warning("summarization failed; replaying full history unchanged")
        return history

    synthetic = [
        ("user", f"[Summary of earlier conversation]\n{summary_text}"),
        ("assistant", "Understood, I have the prior context."),
    ]
    return synthetic + to_keep
