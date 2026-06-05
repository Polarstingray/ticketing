"""Thin REST client for the Stingray Tickets API.

Mirrors the endpoints documented in ../api_guide.md. Auth is the X-API-Key
header for the claude-bot user. Only the calls the resolver needs are wrapped.
"""
from __future__ import annotations

from typing import Any, Iterator

import requests


class StingrayClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"X-API-Key": api_key, "Content-Type": "application/json"}
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        resp = self.session.request(method, self._url(path), timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        return resp

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
