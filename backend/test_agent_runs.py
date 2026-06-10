"""Agent-run sub-resource: the resolver POSTs per-phase token usage/cost, the
UI lists it. Mirrors the activity sub-resource conventions in test_tickets.py.

Shares one database with the rest of the suite, so assertions check membership
by id rather than absolute counts.
"""


def _create(client, key, **overrides):
    body = {"type": "task", "title": "A ticket"}
    body.update(overrides)
    r = client.post("/tickets", json=body, headers={"X-API-Key": key})
    assert r.status_code == 201, r.text
    return r.json()


def _run_payload(**overrides):
    body = {
        "agent": "claude",
        "phase": "plan",
        "model": "claude-opus-4-8",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 50,
        "cache_write_tokens": 10,
        "cost_usd": 0.0123,
        "status": "succeeded",
    }
    body.update(overrides)
    return body


def test_post_and_list_agent_run(client, admin_key):
    t = _create(client, admin_key)
    r = client.post(
        f"/tickets/{t['id']}/agent-runs",
        json=_run_payload(),
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["ticket_id"] == t["id"]
    assert run["phase"] == "plan"
    assert run["agent"] == "claude"
    assert run["input_tokens"] == 1000
    assert run["cost_usd"] == 0.0123
    assert run["finished_at"].endswith(("Z", "+00:00"))  # UTC-aware

    r = client.get(f"/tickets/{t['id']}/agent-runs", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    ids = [x["id"] for x in r.json()]
    assert run["id"] in ids


def test_list_ordered_by_started_then_id(client, admin_key):
    t = _create(client, admin_key)
    for phase, ts in (
        ("plan", "2026-01-01T00:00:00Z"),
        ("implement", "2026-01-01T01:00:00Z"),
        ("review", "2026-01-01T02:00:00Z"),
    ):
        r = client.post(
            f"/tickets/{t['id']}/agent-runs",
            json=_run_payload(phase=phase, started_at=ts),
            headers={"X-API-Key": admin_key},
        )
        assert r.status_code == 201, r.text

    runs = client.get(
        f"/tickets/{t['id']}/agent-runs", headers={"X-API-Key": admin_key}
    ).json()
    assert [x["phase"] for x in runs] == ["plan", "implement", "review"]


def test_finished_at_defaults_when_omitted(client, admin_key):
    t = _create(client, admin_key)
    payload = _run_payload()
    payload.pop("status")  # exercise schema defaults too
    r = client.post(
        f"/tickets/{t['id']}/agent-runs", json=payload, headers={"X-API-Key": admin_key}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["finished_at"]  # server filled it in
    assert body["status"] == "succeeded"


def test_bad_phase_rejected_422(client, admin_key):
    t = _create(client, admin_key)
    r = client.post(
        f"/tickets/{t['id']}/agent-runs",
        json=_run_payload(phase="deploy"),
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 422


def test_bad_agent_rejected_422(client, admin_key):
    t = _create(client, admin_key)
    r = client.post(
        f"/tickets/{t['id']}/agent-runs",
        json=_run_payload(agent="gpt"),
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 422


def test_post_missing_ticket_404(client, admin_key):
    r = client.post(
        "/tickets/999999/agent-runs",
        json=_run_payload(),
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 404


def test_outsider_cannot_post_or_list(client, admin_key, make_user):
    """A member who can neither modify nor view the ticket is blocked: POST is
    403 (can't forge runs) and GET is 404 (can't even confirm it exists)."""
    t = _create(client, admin_key)
    outsider = make_user()
    r = client.post(
        f"/tickets/{t['id']}/agent-runs",
        json=_run_payload(),
        headers={"X-API-Key": outsider.key},
    )
    assert r.status_code == 403
    r = client.get(
        f"/tickets/{t['id']}/agent-runs", headers={"X-API-Key": outsider.key}
    )
    assert r.status_code == 404


def test_assignee_can_post(client, admin_key, make_user):
    """The resolver posts as the ticket's assignee (claude-bot); the assignee
    passes can_modify_ticket even as a non-admin member."""
    bot = make_user()
    t = _create(client, admin_key, assigned_to=bot.id)
    r = client.post(
        f"/tickets/{t['id']}/agent-runs",
        json=_run_payload(phase="implement"),
        headers={"X-API-Key": bot.key},
    )
    assert r.status_code == 201, r.text


def test_delete_ticket_cascades_runs(client, admin_key):
    t = _create(client, admin_key)
    r = client.post(
        f"/tickets/{t['id']}/agent-runs",
        json=_run_payload(),
        headers={"X-API-Key": admin_key},
    )
    assert r.status_code == 201
    assert client.delete(
        f"/tickets/{t['id']}", headers={"X-API-Key": admin_key}
    ).status_code == 204
    # Ticket gone -> the runs sub-resource 404s with it (and the rows are gone).
    r = client.get(f"/tickets/{t['id']}/agent-runs", headers={"X-API-Key": admin_key})
    assert r.status_code == 404
