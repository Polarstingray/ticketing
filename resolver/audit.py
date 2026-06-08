"""Central logging + audit trail for the resolver.

Configures a single `"resolver"` logger that fans out to three places:

  * **stdout** — INFO and up, so the existing `cron.log` keeps its summary.
  * **logs/sweep-<id>.log** — the full human-readable trace at DEBUG.
  * **logs/audit-<id>.jsonl** — one structured JSON object per *audit* event
    (every subprocess, every API call, every Claude tool use, every state
    transition), for later grep/analysis.

Everything is run through `redact()` first so the X-API-Key and token-shaped
strings never reach disk. A context var carries the current ticket id onto every
record so a multi-ticket sweep stays filterable.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

LOGGER_NAME = "resolver"

# Current ticket id, stamped onto every log record. Updated at the top of
# process(); "-" outside any ticket (sweep start/end, pruning).
_ticket_ctx: ContextVar[str] = ContextVar("resolver_ticket", default="-")

# --- redaction -----------------------------------------------------------
_REDACTED = "«redacted»"
_literal_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register an exact secret string (e.g. the API key) to scrub from all
    output. Short values are ignored to avoid mangling unrelated text."""
    if value and len(value) >= 6:
        _literal_secrets.add(value)


def redact(text: str) -> str:
    """Remove registered secrets and token-shaped strings from a log line."""
    if not text:
        return text
    for secret in _literal_secrets:
        if secret in text:
            text = text.replace(secret, _REDACTED)
    # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_) and Stingray keys (sk_...).
    text = re.sub(r"\bgh[poursa]_[A-Za-z0-9]{16,}", _REDACTED, text)
    text = re.sub(r"\bsk_[A-Za-z0-9]{8,}", _REDACTED, text)
    # `Bearer <token>` and `X-API-Key: <token>` — keep the label, drop the value.
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1" + _REDACTED, text)
    text = re.sub(r"(?i)(x-api-key['\"\s:=]+)[A-Za-z0-9._\-]{8,}", r"\1" + _REDACTED, text)
    return text


def clip(text: str | None, limit: int) -> str:
    """Tail-truncate text to `limit` characters for the audit payload."""
    text = (text or "").strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]


# --- formatters / filters ------------------------------------------------
class _HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.ticket = _ticket_ctx.get()
        return redact(super().format(record))


class _AuditFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = getattr(record, "audit", None) or {}
        obj = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticket": _ticket_ctx.get(),
            "level": record.levelname,
            **payload,
        }
        return redact(json.dumps(obj, ensure_ascii=False, default=str))


class _AuditOnly(logging.Filter):
    """Pass only records that carry a structured `audit` payload."""

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "audit", None) is not None


# --- public API ----------------------------------------------------------
def setup_logging(cfg, sweep_id: str) -> logging.Logger:
    """Configure and return the resolver logger for one sweep."""
    register_secret(cfg.api_key)
    cfg.logs_dir.mkdir(exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):  # idempotent across re-setup (e.g. tests)
        logger.removeHandler(h)

    human_fmt = _HumanFormatter("[%(asctime)s] %(levelname)s #%(ticket)s %(message)s", "%H:%M:%S")

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(human_fmt)
    stdout.setLevel(logging.INFO)
    logger.addHandler(stdout)

    human_file = logging.FileHandler(
        cfg.logs_dir / f"sweep-{sweep_id}.log", encoding="utf-8", delay=True
    )
    human_file.setFormatter(human_fmt)
    human_file.setLevel(logging.DEBUG)
    logger.addHandler(human_file)

    audit_file = logging.FileHandler(
        cfg.logs_dir / f"audit-{sweep_id}.jsonl", encoding="utf-8", delay=True
    )
    audit_file.setFormatter(_AuditFormatter())
    audit_file.addFilter(_AuditOnly())
    audit_file.setLevel(logging.DEBUG)
    logger.addHandler(audit_file)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def set_ticket(ticket_id: Any) -> None:
    """Stamp subsequent log records with this ticket id (or '-' when None)."""
    _ticket_ctx.set(str(ticket_id) if ticket_id is not None else "-")


def audit_event(logger: logging.Logger, kind: str, message: str,
                level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured audit line (→ JSONL) plus a human-readable line.

    `kind` is the event class: 'subprocess' | 'api' | 'agent_tool' | 'phase'.
    """
    logger.log(level, message, extra={"audit": {"kind": kind, **fields}})


def prune_old_logs(cfg, logger: logging.Logger | None = None) -> int:
    """Delete sweep/audit/per-ticket logs older than cfg.log_retention_days.
    Returns the number removed. A non-positive retention disables pruning."""
    if cfg.log_retention_days <= 0:
        return 0
    cutoff = time.time() - cfg.log_retention_days * 86400
    removed = 0
    for pattern in ("sweep-*.log", "audit-*.jsonl", "ticket-*-*.log"):
        for path in cfg.logs_dir.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    if logger and removed:
        logger.info("pruned %d log file(s) older than %d days", removed, cfg.log_retention_days)
    return removed
