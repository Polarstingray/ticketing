"""Thin REST client for the Stingray Tickets API.

Mirrors the endpoints documented in ``api_guide.md``. Auth is the ``X-API-Key``
header.

This is the shared implementation used by both the ``stingray`` CLI and the
resolver. It deliberately depends on nothing but ``requests``: the resolver's
audit logging is layered on by overriding :meth:`StingrayClient._audit` (see
``resolver/stingray.py``), which is a no-op here.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Iterator

import requests

# Transient HTTP statuses worth retrying. Other 4xx are deterministic client
# errors (bad request, not found, unauthorized) and must not be retried.
_RETRY_STATUS = {429, 500, 502, 503, 504}


class NotJsonError(Exception):
    """The server answered, but not with JSON.

    Almost always a base-URL mistake: the default deployment serves the SPA at
    the root and proxies the API under ``/api``, so pointing at the root gets a
    200 of ``index.html``. Decoding that raised "Expecting value: line 1
    column 1", which reads like a parse bug rather than a wrong URL.
    """



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
        """Hook called once per HTTP attempt. No-op by default.

        The resolver overrides this to emit structured audit events; the CLI has
        no audit log, so nothing here should be required for correctness.
        """

    def _check_json(self, resp: requests.Response) -> None:
        """Fail loudly when a 2xx isn't JSON — every endpoint here returns JSON.

        Catching it at the transport layer means the diagnosis names the actual
        problem (wrong base URL) instead of surfacing a decode error from
        whichever call site happened to run first.
        """
        content_type = resp.headers.get("Content-Type", "").lower()
        # Only fire on an *affirmative* non-JSON type. A missing header proves
        # nothing (204s and some proxies omit it) and json() will raise on its
        # own if the body really is junk — refusing here would reject responses
        # that are perfectly fine.
        if not content_type or "json" in content_type:
            return
        hint = ""
        if "html" in content_type:
            hint = (
                " That looks like a web page, not the API — check the base URL. "
                "The default deployment serves the app at the root and the API "
                "under /api, e.g. http://localhost:3000/api"
            )
        url = getattr(resp, "url", self.base_url)
        raise NotJsonError(f"{url} returned {content_type}, expected JSON.{hint}")

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
            self._check_json(resp)
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

    def create_agent_run(self, ticket_id: int, **fields: Any) -> dict:
        """Record one resolver phase (agent, phase, model, token usage, cost,
        status) against a ticket. Datetimes are passed as ISO-8601 strings.
        Reuses the retry/backoff/audit wrapper."""
        return self._request(
            "POST", f"/tickets/{ticket_id}/agent-runs", json=fields
        ).json()

    def list_agent_runs(self, ticket_id: int) -> list[dict]:
        return self._request("GET", f"/tickets/{ticket_id}/agent-runs").json()

    def cost_rollup(self, ticket_id: int) -> dict:
        """A ticket's own agent-run cost plus that of its delegated children."""
        return self._request("GET", f"/tickets/{ticket_id}/cost-rollup").json()

    # --- identity --------------------------------------------------------
    def whoami(self) -> dict:
        """The user this key authenticates as. Used by `stingray auth login` to
        validate a key before storing it."""
        return self._request("GET", "/auth/me").json()

    # --- resolver registry ----------------------------------------------
    def heartbeat(self, **fields: Any) -> dict:
        """Self-report this resolver's identity + observed state for the manager
        registry (label, name, agent, model, effective_config). Authenticates as
        the bot's own user; the server keys the row by that user id. Reuses the
        retry/backoff/audit wrapper."""
        return self._request("POST", "/resolvers/heartbeat", json=fields).json()

    # --- resolver settings ----------------------------------------------
    def get_resolver_settings(self, bot_user_id: int | None = None) -> dict:
        """Server-managed, non-secret resolver tunables for this identity.
        Returns the {bot_user_id, settings, secrets, ...} envelope; the caller
        overlays ``settings`` onto its .env-derived config. Reuses the
        retry/backoff/audit wrapper."""
        params = {"bot_user_id": bot_user_id} if bot_user_id is not None else None
        return self._request("GET", "/resolver-settings", params=params).json()

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
