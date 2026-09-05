"""The heartbeat's forward/backward compatibility with the server it talks to.

The resolver and the backend are deployed one at a time, so a worker regularly
runs against a server older than itself. The endpoint forbids unknown fields on
purpose — that is what stops a config snapshot smuggling a secret — which makes
"new worker, old server" a 422 on every beat unless the client handles it.
"""
import json

import pytest
import requests

from stingray_client.api import StingrayClient


class _Response:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)
        self.headers = {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


@pytest.fixture
def client(monkeypatch):
    """A client whose transport records what it was asked to send."""
    sent: list[dict] = []
    api = StingrayClient("http://stingray.test/api", "sk_test", max_retries=1)

    def request(method, url, **kwargs):
        sent.append(dict(kwargs.get("json") or {}))
        return request.answers.pop(0)

    request.answers = []
    monkeypatch.setattr(api.session, "request", request)
    api._sent = sent
    api._answers = request
    return api


def test_a_current_server_gets_every_field(client):
    client._answers.answers = [_Response(200, {"bot_user_id": 3})]
    client.heartbeat(name="gemini", station="ubvm.home.lab", heartbeat_seconds=300)
    assert client._sent[0]["station"] == "ubvm.home.lab"
    assert len(client._sent) == 1


def test_an_older_server_gets_a_second_try_without_the_new_fields(client):
    """422 means "I do not know that key" — not "your worker is broken".

    Without this the roster would show every upgraded worker as never-seen
    until the backend caught up, which is the opposite of what a liveness
    feature is for.
    """
    client._answers.answers = [_Response(422), _Response(200, {"bot_user_id": 3})]
    client.heartbeat(name="gemini", agent="opencode",
                     station="ubvm.home.lab", heartbeat_seconds=300)

    assert len(client._sent) == 2
    assert "station" in client._sent[0]
    # The retry keeps the identity fields and drops only what the old server
    # cannot know about.
    assert client._sent[1] == {"name": "gemini", "agent": "opencode"}


def test_a_422_on_fields_the_server_does_understand_still_raises(client):
    """Otherwise a real validation error would be retried into silence."""
    client._answers.answers = [_Response(422)]
    with pytest.raises(requests.HTTPError):
        client.heartbeat(name="gemini", agent="opencode")
    assert len(client._sent) == 1      # no pointless second attempt


def test_other_errors_are_not_swallowed(client):
    client._answers.answers = [_Response(403)]
    with pytest.raises(requests.HTTPError):
        client.heartbeat(name="gemini", station="ubvm.home.lab")
    assert len(client._sent) == 1
