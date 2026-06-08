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

import fcntl
import json
import logging
import os
import re
import sys
import tarfile
import time
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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


# Loose per-sweep / per-ticket log files this lifecycle manages. The cron
# stdout logs and the archive/ tarballs are handled separately.
_LOOSE_PATTERNS = ("sweep-*.log", "audit-*.jsonl", "ticket-*-*.log")


def discard_sweep_logs(cfg, sweep_id: str) -> int:
    """Drop this sweep's own `sweep-<id>.log` + `audit-<id>.jsonl` — called when
    a sweep processed no tickets, so empty sweeps leave no files behind. Closes
    the matching FileHandlers first so the unlink is clean. Returns count removed."""
    targets = {
        str(cfg.logs_dir / f"sweep-{sweep_id}.log"),
        str(cfg.logs_dir / f"audit-{sweep_id}.jsonl"),
    }
    logger = logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) in targets:
            h.close()
            logger.removeHandler(h)
    removed = 0
    for name in targets:
        try:
            Path(name).unlink()
            removed += 1
        except OSError:
            pass  # never created (delay=True and no write) — fine
    return removed


def archive_old_logs(cfg, logger: logging.Logger | None = None) -> int:
    """Roll loose logs from days older than cfg.log_archive_after_days into one
    gzipped tarball per day under logs/archive/, then unlink the originals.
    flock-guarded so two bots sharing the dir don't double-archive. Returns the
    number of tarballs written."""
    if cfg.log_archive_after_days <= 0:
        return 0
    logs_dir: Path = cfg.logs_dir
    cutoff_day = date.today() - timedelta(days=cfg.log_archive_after_days)

    by_day: dict[str, list[Path]] = {}
    for pattern in _LOOSE_PATTERNS:
        for path in logs_dir.glob(pattern):
            try:
                day = date.fromtimestamp(path.stat().st_mtime)
            except OSError:
                continue
            if day <= cutoff_day:
                by_day.setdefault(day.isoformat(), []).append(path)
    if not by_day:
        return 0

    archive_dir = logs_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    lock_path = logs_dir / ".archive.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another sweep is archiving; skip this tick

        written = 0
        for day, paths in sorted(by_day.items()):
            tarball = archive_dir / f"{day}.tar.gz"
            if tarball.exists():
                continue  # already archived; leave stragglers to prune's safety net
            tmp = tarball.with_suffix(".tar.gz.tmp")
            try:
                with tarfile.open(tmp, "w:gz") as tar:
                    for p in paths:
                        tar.add(p, arcname=p.name)
                os.replace(tmp, tarball)
            except OSError:
                tmp.unlink(missing_ok=True)
                continue
            for p in paths:
                try:
                    p.unlink()
                except OSError:
                    pass
            written += 1
        if logger and written:
            logger.info("archived %d day(s) of logs into %s", written, archive_dir)
        return written


def prune_old_logs(cfg, logger: logging.Logger | None = None) -> int:
    """Delete archived tarballs (and any stray loose logs) older than
    cfg.log_retention_days. Returns the number removed. Non-positive disables it."""
    if cfg.log_retention_days <= 0:
        return 0
    cutoff = time.time() - cfg.log_retention_days * 86400
    removed = 0
    for path in cfg.logs_dir.glob("archive/*.tar.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    for pattern in _LOOSE_PATTERNS:  # safety net for loose files that escaped archiving
        for path in cfg.logs_dir.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    if logger and removed:
        logger.info("pruned %d archived/old log file(s) older than %d days",
                    removed, cfg.log_retention_days)
    return removed


def rotate_cron_log(cfg, logger: logging.Logger | None = None) -> bool:
    """Size-rotate this resolver's cron stdout log to <path>.1 when it exceeds
    cfg.cron_log_max_bytes. Renaming (not truncating) is safe under cron's `>>`:
    the running process keeps writing into the renamed inode and the next tick
    opens a fresh file. Keeps one backup. Returns True if it rotated."""
    path: Path | None = cfg.cron_log
    if path is None or cfg.cron_log_max_bytes <= 0:
        return False
    try:
        if path.stat().st_size <= cfg.cron_log_max_bytes:
            return False
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        return False
    if logger:
        logger.info("rotated cron log %s (> %d bytes)", path.name, cfg.cron_log_max_bytes)
    return True


def maintain_logs(cfg, logger: logging.Logger | None = None) -> None:
    """One-call log lifecycle at sweep start: archive finished days into daily
    tarballs, delete archives past retention, and size-rotate the cron log."""
    archive_old_logs(cfg, logger)
    prune_old_logs(cfg, logger)
    rotate_cron_log(cfg, logger)
