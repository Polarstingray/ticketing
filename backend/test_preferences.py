"""Tests for per-user notification preferences (the settings panel, #55).

Covers: the default-on matrix returned by GET; PUT upserting opt-outs and
pruning rows that return to the default; that the table only ever stores explicit
overrides; that should_notify gates the in-app path; and per-user scoping.
"""
from types import SimpleNamespace

from database import SessionLocal
from inbox import should_notify
from models import NotificationPreference, Ticket, User
from notifications import notify_comment_email


def _hdr(key):
    return {"X-API-Key": key}


def _create_ticket(client, key, **kw):
    body = {"type": "task", "title": "T", **kw}
    r = client.post("/tickets", headers=_hdr(key), json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- GET: default-on matrix --------------------------------------------------

def test_get_returns_full_matrix_default_on(client, make_user):
    u = make_user()
    r = client.get("/preferences", headers=_hdr(u.key))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    # 2 types x 2 channels.
    assert len(items) == 4
    assert all(it["enabled"] is True for it in items)
    combos = {(it["type"], it["channel"]) for it in items}
    assert combos == {
        ("assigned", "in_app"), ("assigned", "email"),
        ("commented", "in_app"), ("commented", "email"),
    }


# --- PUT: upsert + prune -----------------------------------------------------

def test_put_disables_and_persists(client, make_user):
    u = make_user()
    r = client.put(
        "/preferences", headers=_hdr(u.key),
        json={"items": [{"type": "assigned", "channel": "email", "enabled": False}]},
    )
    assert r.status_code == 200, r.text
    by_combo = {(it["type"], it["channel"]): it["enabled"] for it in r.json()["items"]}
    assert by_combo[("assigned", "email")] is False
    # Untouched combos stay on.
    assert by_combo[("assigned", "in_app")] is True

    # Re-read independently.
    again = client.get("/preferences", headers=_hdr(u.key)).json()["items"]
    assert {(i["type"], i["channel"]): i["enabled"] for i in again}[("assigned", "email")] is False


def test_put_enabling_prunes_the_row(client, make_user):
    u = make_user()
    # Disable, then re-enable: the explicit row should be deleted (default-on).
    client.put("/preferences", headers=_hdr(u.key),
               json={"items": [{"type": "commented", "channel": "in_app", "enabled": False}]})
    db = SessionLocal()
    try:
        assert db.query(NotificationPreference).filter(
            NotificationPreference.user_id == u.id).count() == 1
    finally:
        db.close()

    client.put("/preferences", headers=_hdr(u.key),
               json={"items": [{"type": "commented", "channel": "in_app", "enabled": True}]})
    db = SessionLocal()
    try:
        assert db.query(NotificationPreference).filter(
            NotificationPreference.user_id == u.id).count() == 0
    finally:
        db.close()


def test_put_rejects_unknown_type_or_channel(client, make_user):
    u = make_user()
    r = client.put("/preferences", headers=_hdr(u.key),
                   json={"items": [{"type": "nope", "channel": "in_app", "enabled": False}]})
    assert r.status_code == 422


# --- should_notify gating + scoping ------------------------------------------

def test_should_notify_reflects_preference(client, make_user):
    u = make_user()
    db = SessionLocal()
    try:
        # Default-on with no rows.
        assert should_notify(db, u.id, "assigned", "in_app") is True
        assert should_notify(db, u.id, "assigned", "email") is True
    finally:
        db.close()

    client.put("/preferences", headers=_hdr(u.key),
               json={"items": [{"type": "assigned", "channel": "in_app", "enabled": False}]})
    db = SessionLocal()
    try:
        assert should_notify(db, u.id, "assigned", "in_app") is False
        # Other channel unaffected.
        assert should_notify(db, u.id, "assigned", "email") is True
    finally:
        db.close()


def test_in_app_notification_suppressed_when_opted_out(client, admin_key, make_user):
    u = make_user()
    # Opt out of in-app assignment notifications.
    client.put("/preferences", headers=_hdr(u.key),
               json={"items": [{"type": "assigned", "channel": "in_app", "enabled": False}]})
    # Admin assigns a ticket to u -> no in-app notification should be created.
    _create_ticket(client, admin_key, title="Silent", assigned_to=u.id)
    data = client.get("/notifications", headers=_hdr(u.key)).json()
    assert all(n["ticket_title"] != "Silent" for n in data["items"])


class _RecordingBackground:
    """Stand-in for FastAPI's BackgroundTasks that records add_task calls."""

    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


def test_comment_email_suppressed_when_opted_out(client, make_user):
    actor = make_user()
    recipient = make_user()
    # recipient opts out of email for new comments.
    client.put("/preferences", headers=_hdr(recipient.key),
               json={"items": [{"type": "commented", "channel": "email", "enabled": False}]})

    db = SessionLocal()
    try:
        # The unit-level gate is what notify_comment_email consults.
        assert should_notify(db, recipient.id, "commented", "email") is False

        actor_user = db.query(User).filter(User.id == actor.id).first()
        ticket = Ticket(
            type="task", title="Emailed?", created_by=recipient.id,
            assigned_to=None, priority="medium", status="open",
        )
        db.add(ticket)
        db.flush()
        comment = SimpleNamespace(id=1, body="hi")

        bg = _RecordingBackground()
        notify_comment_email(bg, db, ticket, comment, actor_user)
        # recipient is opted out -> no email task scheduled.
        assert bg.tasks == []
    finally:
        db.rollback()
        db.close()


def test_comment_email_scheduled_when_opted_in(client, make_user):
    actor = make_user()
    recipient = make_user()  # default-on, no preference rows.

    db = SessionLocal()
    try:
        assert should_notify(db, recipient.id, "commented", "email") is True

        actor_user = db.query(User).filter(User.id == actor.id).first()
        ticket = Ticket(
            type="task", title="Emailed?", created_by=recipient.id,
            assigned_to=None, priority="medium", status="open",
        )
        db.add(ticket)
        db.flush()
        comment = SimpleNamespace(id=1, body="hi")

        bg = _RecordingBackground()
        notify_comment_email(bg, db, ticket, comment, actor_user)
        # One email task for the opted-in recipient.
        assert len(bg.tasks) == 1
        func, args, _ = bg.tasks[0]
        # args = (to_list, subject, body)
        assert args[0] == [recipient.username + "@example.com"]
    finally:
        db.rollback()
        db.close()


def test_preferences_are_per_user(client, make_user):
    a = make_user()
    b = make_user()
    client.put("/preferences", headers=_hdr(a.key),
               json={"items": [{"type": "assigned", "channel": "email", "enabled": False}]})
    # b's matrix is untouched (all on).
    items = client.get("/preferences", headers=_hdr(b.key)).json()["items"]
    assert all(it["enabled"] is True for it in items)
