"""Webhook subscriptions: CRUD, secret hygiene, SSRF rejection, delivery log.

DNS is monkeypatched throughout (``_public_dns``): the suite must never make a
real lookup, and the SSRF checks are about *what a host resolves to*, which is
exactly what has to be controlled to test them.
"""
import socket

import pytest

import webhook_urls
from database import SessionLocal
from models import DeliveryState, Webhook, WebhookDelivery

HOOK_URL = "https://hooks.example.com/stingray"


def _addrinfo(*addresses):
    """Shape ``socket.getaddrinfo`` returns: (family, type, proto, canon, sockaddr)."""
    out = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 0, 0, 0) if family == socket.AF_INET6 else (address, 0)
        out.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
    return out


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Every hostname resolves to one public address unless a test says otherwise."""
    monkeypatch.setattr(
        webhook_urls.socket,
        "getaddrinfo",
        lambda host, *a, **kw: _addrinfo("93.184.216.34"),
    )


def _auth(user):
    return {"X-API-Key": user.key}


def _create(client, user, **overrides):
    body = {"name": "ci", "url": HOOK_URL}
    body.update(overrides)
    res = client.post("/webhooks", json=body, headers=_auth(user))
    assert res.status_code == 201, res.text
    return res.json()


# --- Secret hygiene (the named requirement) ----------------------------------

def test_secret_shown_once_and_never_on_reads(client, make_user):
    user = make_user()
    created = _create(client, user, name="secret-hygiene")
    secret = created["secret"]
    assert secret and created["secret_prefix"] == secret[:8]

    listed = client.get("/webhooks", headers=_auth(user))
    fetched = client.get(f"/webhooks/{created['id']}", headers=_auth(user))
    patched = client.patch(
        f"/webhooks/{created['id']}", json={"name": "renamed"}, headers=_auth(user)
    )

    for res in (listed, fetched, patched):
        assert res.status_code == 200, res.text
        # Neither the field nor the value, however the body is composed.
        assert secret not in res.text
    assert "secret" not in fetched.json()
    assert "secret" not in patched.json()
    assert all("secret" not in item for item in listed.json())
    # The non-secret label is still there for the UI.
    assert fetched.json()["secret_prefix"] == secret[:8]


def test_rotate_secret_issues_a_new_one(client, make_user):
    user = make_user()
    created = _create(client, user, name="rotate")
    old = created["secret"]

    res = client.post(f"/webhooks/{created['id']}/rotate-secret", headers=_auth(user))
    assert res.status_code == 200, res.text
    new = res.json()["secret"]
    assert new != old
    assert res.json()["secret_prefix"] == new[:8]

    after = client.get(f"/webhooks/{created['id']}", headers=_auth(user))
    assert after.json()["secret_prefix"] == new[:8]
    assert old not in after.text and new not in after.text


# --- CRUD --------------------------------------------------------------------

def test_crud_roundtrip(client, make_user):
    user = make_user()
    created = _create(
        client,
        user,
        name="  ci hook  ",
        event_types=["ticket.created", "comment.created"],
        tag_filter=["repo:foo"],
    )
    assert created["name"] == "ci hook"  # trimmed
    assert created["event_types"] == ["ticket.created", "comment.created"]
    assert created["tag_filter"] == ["repo:foo"]
    assert created["active"] is True
    assert created["consecutive_failures"] == 0

    listed = client.get("/webhooks", headers=_auth(user)).json()
    assert [w["id"] for w in listed] == [created["id"]]

    patched = client.patch(
        f"/webhooks/{created['id']}",
        json={"active": False, "event_types": [], "tag_filter": []},
        headers=_auth(user),
    ).json()
    assert patched["active"] is False
    assert patched["event_types"] == [] and patched["tag_filter"] == []

    assert client.delete(f"/webhooks/{created['id']}", headers=_auth(user)).status_code == 204
    assert client.get(f"/webhooks/{created['id']}", headers=_auth(user)).status_code == 404


def test_reactivating_resets_the_failure_streak(client, make_user):
    user = make_user()
    created = _create(client, user, name="streak", active=False)
    db = SessionLocal()
    try:
        db.query(Webhook).filter(Webhook.id == created["id"]).update(
            {"consecutive_failures": 7}
        )
        db.commit()
    finally:
        db.close()

    res = client.patch(
        f"/webhooks/{created['id']}", json={"active": True}, headers=_auth(user)
    )
    assert res.json()["consecutive_failures"] == 0


def test_per_user_cap(client, make_user, monkeypatch):
    user = make_user()
    monkeypatch.setattr("routers.webhooks.MAX_WEBHOOKS_PER_USER", 2)
    _create(client, user, name="one")
    _create(client, user, name="two")
    res = client.post(
        "/webhooks", json={"name": "three", "url": HOOK_URL}, headers=_auth(user)
    )
    assert res.status_code == 422
    assert "Too many webhooks" in res.text


def test_another_users_webhook_is_404_everywhere(client, make_user):
    owner, other = make_user(), make_user()
    created = _create(client, owner, name="theirs")
    wid = created["id"]

    assert client.get(f"/webhooks/{wid}", headers=_auth(other)).status_code == 404
    assert client.patch(
        f"/webhooks/{wid}", json={"name": "x"}, headers=_auth(other)
    ).status_code == 404
    assert client.delete(f"/webhooks/{wid}", headers=_auth(other)).status_code == 404
    assert client.post(
        f"/webhooks/{wid}/rotate-secret", headers=_auth(other)
    ).status_code == 404
    assert client.get(f"/webhooks/{wid}/deliveries", headers=_auth(other)).status_code == 404
    assert client.post(
        f"/webhooks/{wid}/deliveries/1/redeliver", headers=_auth(other)
    ).status_code == 404
    # And it is not visible in their listing, even with ?user_id= aimed at the owner.
    listed = client.get(f"/webhooks?user_id={owner.id}", headers=_auth(other)).json()
    assert all(w["id"] != wid for w in listed)


def test_requires_authentication(client):
    assert client.get("/webhooks").status_code == 401
    assert client.post("/webhooks", json={"name": "x", "url": HOOK_URL}).status_code == 401


# --- SSRF --------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",                        # http scheme + loopback
        "https://127.0.0.1/hook",                       # loopback literal
        "http://169.254.169.254/latest/meta-data/",     # AWS metadata
        "https://169.254.169.254/computeMetadata/v1/",  # metadata over https
        "https://10.0.0.5/hook",                        # RFC1918
        "https://192.168.1.1/hook",                     # RFC1918
        "https://172.16.0.1/hook",                      # RFC1918
        "https://[::1]/hook",                           # IPv6 loopback
        "https://[fd00:ec2::254]/hook",                 # IPv6 unique-local metadata
        "https://[::]/hook",                            # unspecified
        "https://metadata.google.internal/x",           # GCP metadata by name
        "https://build-box.internal/hook",              # internal suffix
        "https://printer.local/hook",                   # mDNS suffix
        "https://localhost/hook",                       # by name
        "ftp://example.com/hook",                       # scheme
        "https://user:pass@example.com/hook",           # userinfo
        "https://example.com:22/hook",                  # disallowed port
        "https://example.com/hook#frag",                # fragment
        "not a url",                                    # unparseable / no host
        "https://example.com/" + "a" * 2100,            # over-long
    ],
)
def test_rejects_unsafe_urls(client, make_user, url):
    user = make_user()
    res = client.post("/webhooks", json={"name": "bad", "url": url}, headers=_auth(user))
    assert res.status_code == 422, f"{url} was accepted: {res.text}"
    assert webhook_urls.SSRF_DENY_REASON in res.text


def test_patch_rejects_unsafe_url(client, make_user):
    user = make_user()
    created = _create(client, user, name="patch-ssrf")
    res = client.patch(
        f"/webhooks/{created['id']}",
        json={"url": "https://169.254.169.254/"},
        headers=_auth(user),
    )
    assert res.status_code == 422
    assert client.get(f"/webhooks/{created['id']}", headers=_auth(user)).json()["url"] == HOOK_URL


def test_accepts_a_public_host(client, make_user):
    user = make_user()
    created = _create(client, user, name="public", url="https://example.com:8443/hook")
    assert created["url"] == "https://example.com:8443/hook"


def test_every_resolved_address_must_pass(monkeypatch):
    """One public answer beside a private one is still a rejection — checking
    only the first address is checking nothing (DNS rebinding)."""
    monkeypatch.setattr(
        webhook_urls.socket,
        "getaddrinfo",
        lambda host, *a, **kw: _addrinfo("93.184.216.34", "10.1.2.3"),
    )
    with pytest.raises(ValueError, match="private address"):
        webhook_urls.validate_webhook_url("https://sneaky.example.com/hook")


def test_dns_failure_is_a_rejection(monkeypatch):
    def _boom(*a, **kw):
        raise socket.gaierror("nope")

    monkeypatch.setattr(webhook_urls.socket, "getaddrinfo", _boom)
    with pytest.raises(ValueError, match="does not resolve"):
        webhook_urls.validate_webhook_url("https://nx.example.com/hook")


def test_http_allowed_only_behind_the_env_flag(monkeypatch):
    with pytest.raises(ValueError, match="scheme must be https"):
        webhook_urls.validate_webhook_url("http://example.com/hook")

    monkeypatch.setenv("ALLOW_INSECURE_WEBHOOKS", "1")
    assert webhook_urls.validate_webhook_url("http://example.com/hook")
    # The flag relaxes the scheme only — addresses are still checked.
    with pytest.raises(ValueError, match="loopback"):
        webhook_urls.validate_webhook_url("http://127.0.0.1/hook")


# --- Delivery log ------------------------------------------------------------

def _insert_delivery(webhook_id, **fields):
    db = SessionLocal()
    try:
        row = WebhookDelivery(webhook_id=webhook_id, **fields)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _make_ticket(client, admin_key, title, assigned_to=None):
    body = {"type": "task", "title": title, "description": ""}
    if assigned_to is not None:
        body["assigned_to"] = assigned_to
    res = client.post("/tickets", json=body, headers={"X-API-Key": admin_key})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_deliveries_are_filtered_by_owner_visibility(client, make_user, admin_key, admin_id):
    """A member's webhook must not become an exfiltration path: the log shows
    only tickets its *owner* could open — including when an admin reads it."""
    member = make_user()
    webhook = _create(client, member, name="visibility")

    visible = _make_ticket(client, admin_key, "assigned to the member", assigned_to=member.id)
    hidden = _make_ticket(client, admin_key, "admin-only ticket")

    ok_id = _insert_delivery(
        webhook["id"], event_type="ticket.created", ticket_id=visible,
        state=DeliveryState.succeeded.value, status_code=200,
    )
    hidden_id = _insert_delivery(
        webhook["id"], event_type="ticket.created", ticket_id=hidden,
        state=DeliveryState.failed.value,
    )
    ticketless_id = _insert_delivery(webhook["id"], event_type="agent_run.finished")

    res = client.get(f"/webhooks/{webhook['id']}/deliveries", headers=_auth(member))
    assert res.status_code == 200, res.text
    ids = {d["id"] for d in res.json()["items"]}
    assert ok_id in ids and ticketless_id in ids
    assert hidden_id not in ids
    assert res.json()["total"] == 2

    # An admin reading the member's log sees the same — the filter is keyed on
    # the owner, not the caller.
    as_admin = client.get(
        f"/webhooks/{webhook['id']}/deliveries", headers={"X-API-Key": admin_key}
    )
    assert as_admin.status_code == 200
    assert hidden_id not in {d["id"] for d in as_admin.json()["items"]}

    # And a hidden row cannot be re-armed, by either of them.
    for headers in (_auth(member), {"X-API-Key": admin_key}):
        res = client.post(
            f"/webhooks/{webhook['id']}/deliveries/{hidden_id}/redeliver", headers=headers
        )
        assert res.status_code == 404


def test_delivery_listing_filters_and_pagination(client, make_user):
    user = make_user()
    webhook = _create(client, user, name="log")
    # A ticket the owner created, so its deliveries survive the visibility filter.
    own_ticket = _make_ticket(client, user.key, "the owner's own ticket")
    ids = [
        _insert_delivery(
            webhook["id"],
            event_type="ticket.created",
            state=state,
            ticket_id=None if state == DeliveryState.pending.value else own_ticket,
        )
        for state in (
            DeliveryState.pending.value,
            DeliveryState.failed.value,
            DeliveryState.succeeded.value,
        )
    ]

    base = f"/webhooks/{webhook['id']}/deliveries"
    all_rows = client.get(base, headers=_auth(user)).json()
    assert all_rows["total"] == 3
    assert all_rows["limit"] == 50 and all_rows["offset"] == 0
    # Newest first.
    assert [d["id"] for d in all_rows["items"]] == sorted(ids, reverse=True)

    page = client.get(f"{base}?limit=1&offset=1", headers=_auth(user)).json()
    assert len(page["items"]) == 1 and page["total"] == 3
    assert page["items"][0]["id"] == sorted(ids, reverse=True)[1]

    only_failed = client.get(f"{base}?state=failed", headers=_auth(user)).json()
    assert [d["state"] for d in only_failed["items"]] == ["failed"]

    by_ticket = client.get(f"{base}?ticket_id={own_ticket}", headers=_auth(user)).json()
    assert by_ticket["total"] == 2

    assert client.get(f"{base}?state=bogus", headers=_auth(user)).status_code == 422


def test_redeliver_rearms_the_row_and_keeps_history(client, make_user):
    user = make_user()
    webhook = _create(client, user, name="redeliver")
    delivery_id = _insert_delivery(
        webhook["id"],
        event_type="ticket.created",
        state=DeliveryState.failed.value,
        attempt_count=3,
        status_code=500,
        error="connection reset",
    )

    res = client.post(
        f"/webhooks/{webhook['id']}/deliveries/{delivery_id}/redeliver", headers=_auth(user)
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["state"] == "pending"
    assert body["next_attempt_at"] is not None
    assert body["status_code"] is None
    assert body["error"] == ""
    assert body["attempt_count"] == 3  # history is preserved


def test_redeliver_revalidates_the_url(client, make_user, monkeypatch):
    """DNS can move between creation and redelivery; the target is re-checked."""
    user = make_user()
    webhook = _create(client, user, name="rebound")
    delivery_id = _insert_delivery(webhook["id"], event_type="ticket.created")

    monkeypatch.setattr(
        webhook_urls.socket,
        "getaddrinfo",
        lambda host, *a, **kw: _addrinfo("127.0.0.1"),
    )
    res = client.post(
        f"/webhooks/{webhook['id']}/deliveries/{delivery_id}/redeliver", headers=_auth(user)
    )
    assert res.status_code == 422
    assert webhook_urls.SSRF_DENY_REASON in res.text


def test_deleting_a_webhook_cascades_its_log(client, make_user):
    user = make_user()
    webhook = _create(client, user, name="cascade")
    delivery_id = _insert_delivery(webhook["id"], event_type="ticket.created")

    assert client.delete(f"/webhooks/{webhook['id']}", headers=_auth(user)).status_code == 204
    db = SessionLocal()
    try:
        assert db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id).first() is None
    finally:
        db.close()


def test_tag_filter_is_validated_like_ticket_tags(client, make_user):
    user = make_user()
    res = client.post(
        "/webhooks",
        json={"name": "bad-tags", "url": HOOK_URL, "tag_filter": ["ok", "with\nnewline"]},
        headers=_auth(user),
    )
    assert res.status_code == 422


def test_unknown_event_type_is_rejected(client, make_user):
    user = make_user()
    res = client.post(
        "/webhooks",
        json={"name": "bad-event", "url": HOOK_URL, "event_types": ["ticket.exploded"]},
        headers=_auth(user),
    )
    assert res.status_code == 422


def test_event_types_match_what_the_bus_emits():
    """A subscription to an event nobody emits would silently never fire, so the
    enum is pinned to the type list ``events.emit`` documents."""
    import inspect

    import events
    from models import WebhookEventType

    source = inspect.getsource(events)
    for event in WebhookEventType:
        assert f"``{event.value}``" in source, f"{event.value} is not an emitted event"
