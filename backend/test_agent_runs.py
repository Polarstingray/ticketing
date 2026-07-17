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


def test_chat_completion_backends_accepted(client, admin_key):
    """The direct chat-completion backends post runs too: single-shot reviews as
    (review-api, review) and the plan-critique gate as (critique-api, plan-critique).
    Both must be accepted, not 422'd like the agent loops once were."""
    t = _create(client, admin_key)
    for agent, phase in (("review-api", "review"), ("critique-api", "plan-critique")):
        r = client.post(
            f"/tickets/{t['id']}/agent-runs",
            json=_run_payload(agent=agent, phase=phase),
            headers={"X-API-Key": admin_key},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["agent"] == agent and body["phase"] == phase


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


def test_cost_rollup_sums_own_and_children(client, admin_key):
    """The rollup sums a ticket's own runs plus those of any child tagged
    parent:<id>, exposing the whole delegation fan-out's spend."""
    parent = _create(client, admin_key)
    # Parent's own run.
    client.post(
        f"/tickets/{parent['id']}/agent-runs",
        json=_run_payload(phase="plan", cost_usd=0.10, input_tokens=100),
        headers={"X-API-Key": admin_key},
    )
    # A delegated child carries a parent:<id> tag (admin may set reserved tags).
    child = _create(client, admin_key, tags=[f"parent:{parent['id']}"])
    client.post(
        f"/tickets/{child['id']}/agent-runs",
        json=_run_payload(phase="implement", cost_usd=0.25, input_tokens=400),
        headers={"X-API-Key": admin_key},
    )

    r = client.get(f"/tickets/{parent['id']}/cost-rollup", headers={"X-API-Key": admin_key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["own"]["cost_usd"] == 0.10
    assert body["own"]["input_tokens"] == 100
    child_ids = [c["ticket_id"] for c in body["children"]]
    assert child["id"] in child_ids
    assert body["total"]["cost_usd"] == 0.35
    assert body["total"]["input_tokens"] == 500
    assert body["total"]["run_count"] == 2


def test_cost_rollup_no_runs_is_zero(client, admin_key):
    t = _create(client, admin_key)
    body = client.get(
        f"/tickets/{t['id']}/cost-rollup", headers={"X-API-Key": admin_key}
    ).json()
    assert body["own"]["cost_usd"] == 0.0
    assert body["total"]["run_count"] == 0
    assert body["children"] == []


def test_cost_rollup_hidden_from_outsider(client, admin_key, make_user):
    t = _create(client, admin_key)
    outsider = make_user()
    r = client.get(f"/tickets/{t['id']}/cost-rollup", headers={"X-API-Key": outsider.key})
    assert r.status_code == 404


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
