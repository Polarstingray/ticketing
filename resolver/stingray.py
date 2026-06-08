"""Thin REST client for the Stingray Tickets API.

Mirrors the endpoints documented in ../api_guide.md. Auth is the X-API-Key
header for the claude-bot user. Only the calls the resolver needs are wrapped.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Iterator

import requests

from audit import audit_event

# Transient HTTP statuses worth retrying. Other 4xx are deterministic client
# errors (bad request, not found, unauthorized) and must not be retried.
_RETRY_STATUS = {429, 500, 502, 503, 504}


class StingrayClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30,
                 max_retries: int = 3, logger: logging.Logger | None = None,
                 backoff_base: float = 0.5, backoff_cap: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self._logger = logger
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self.session = requests.Session()
        self.session.headers.update(
            {"X-API-Key": api_key, "Content-Type": "application/json"}
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _backoff(self, attempt: int, resp: requests.Response | None) -> None:
        """Sleep before the next retry: exponential backoff + jitter, honoring a
        429's Retry-After when the server provides one."""
        delay = min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_cap)
        if resp is not None and resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
        delay += random.uniform(0, self._backoff_base)
        if delay > 0:
            time.sleep(delay)

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

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Issue a request with bounded retries on transient failures. A network
        wobble or 5xx mid-sweep is retried with backoff instead of throwing —
        which previously left tickets stranded in a claude:* in-flight state."""
        url = self._url(path)
        for attempt in range(1, self.max_retries + 1):
            start = time.monotonic()
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._audit(method, path, kwargs, status=None, attempt=attempt,
                            start=start, error=type(exc).__name__)
                if attempt >= self.max_retries:
                    raise
                self._backoff(attempt, None)
                continue

            retryable = resp.status_code in _RETRY_STATUS and attempt < self.max_retries
            self._audit(method, path, kwargs, status=resp.status_code,
                        attempt=attempt, start=start, retrying=retryable or None)
            if retryable:
                self._backoff(attempt, resp)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("unreachable: retry loop exited without returning")  # pragma: no cover

    # --- tickets ---------------------------------------------------------
    def iter_tickets(self, **filters: Any) -> Iterator[dict]:
        """Yield tickets across all pages for the given filters (e.g.
        assigned_to=, status=). Honors the {items,total,limit,offset} envelope."""
        offset = 0
        limit = 100
        while True:
            params = {**filters, "limit": limit, "offset": offset}
            data = self._request("GET", "/tickets", params=params).json()
            items = data.get("items", [])
            for item in items:
                yield item
            offset += len(items)
            if not items or offset >= data.get("total", 0):
                break

    def create_ticket(self, **fields: Any) -> dict:
        """POST a new ticket and return it. Fields mirror the API (type, title,
        description, priority, tags, code_blocks, assigned_to). The base URL
        already targets the backend directly, so callers never deal with the
        frontend's /api proxy."""
        return self._request("POST", "/tickets", json=fields).json()

    def get_ticket(self, ticket_id: int) -> dict:
        return self._request("GET", f"/tickets/{ticket_id}").json()

    def update_ticket(self, ticket_id: int, **fields: Any) -> dict:
        return self._request("PATCH", f"/tickets/{ticket_id}", json=fields).json()

    # --- comments --------------------------------------------------------
    def list_comments(self, ticket_id: int) -> list[dict]:
        return self._request("GET", f"/tickets/{ticket_id}/comments").json()

    def add_comment(self, ticket_id: int, body: str) -> dict:
        return self._request("POST", f"/tickets/{ticket_id}/comments", json={"body": body}).json()

    def latest_human_comment(self, ticket_id: int, bot_user_id: int) -> dict | None:
        """The most recent comment NOT authored by the bot (comments are
        returned oldest-first)."""
        comments = self.list_comments(ticket_id)
        for comment in reversed(comments):
            if comment.get("author") != bot_user_id:
                return comment
        return None
