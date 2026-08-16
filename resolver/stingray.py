"""Resolver-flavored Stingray API client.

The implementation lives in ``stingray_client.api`` (shared with the ``stingray``
CLI, which has no audit log). This subclass adds the resolver's structured audit
logging back on top, so every call site here keeps working unchanged:

    from stingray import StingrayClient
"""
from __future__ import annotations

import logging
import time
from typing import Any

from audit import audit_event
from stingray_client.api import StingrayClient as _BaseClient


class StingrayClient(_BaseClient):
    def _audit(self, method: str, path: str, kwargs: dict, *, status: int | None,
               attempt: int, start: float, **extra: Any) -> None:
        if self._logger is None:
            return
        fields: dict[str, Any] = {
            "method": method,
            "path": path,
            "status": status,
            "attempt": attempt,
            "duration_ms": round((time.monotonic() - start) * 1000),
        }
        params = kwargs.get("params")
        if params:
            fields["params"] = dict(params)
        body = kwargs.get("json")
        if isinstance(body, dict):
            fields["body_keys"] = sorted(body.keys())
            if "body" in body:  # comment text — record length, never content
                fields["body_len"] = len(str(body.get("body") or ""))
        fields.update(extra)
        level = logging.WARNING if (extra.get("error") or extra.get("retrying")) else logging.DEBUG
        audit_event(self._logger, "api",
                    f"api {method} {path} -> {status} (attempt {attempt})",
                    level=level, **fields)
