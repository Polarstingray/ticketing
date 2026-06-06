"""Tests for optional SMTP email notifications (ticket #4).

Focus areas from the review: the never-raise guarantee (both the low-level
send and the synchronous high-level helpers), the STARTTLS/SSL branching with
TLS verification, header-injection sanitization, and the no-op-when-unconfigured
behaviour.
"""
import os
import ssl
import tempfile

import pytest

# Use an isolated SQLite file before importing app modules (matches the
# project's other test modules).
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

import notifications  # noqa: E402


# --- Test doubles ------------------------------------------------------------

class FakeServer:
    """Records the SMTP interactions a send performs."""

    def __init__(self, kind, host, port, context=None):
        self.kind = kind  # "ssl" or "plain"
        self.host = host
        self.port = port
        self.init_context = context
        self.starttls_context = "unset"
        self.logged_in = None
        self.sent = []
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def captured(monkeypatch):
    """Wire fake SMTP/SMTP_SSL and return a dict recording the server made."""
    box = {"server": None}

    def fake_ssl(host, port, timeout=None, context=None):
        box["server"] = FakeServer("ssl", host, port, context)
        return box["server"]

    def fake_plain(host, port, timeout=None):
        box["server"] = FakeServer("plain", host, port)
        return box["server"]

    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", fake_ssl)
    monkeypatch.setattr(notifications.smtplib, "SMTP", fake_plain)
    return box


def _configure(monkeypatch, **overrides):
    """Set module-level SMTP config (read at import) for a test."""
    defaults = dict(
        SMTP_HOST="mail.example.com",
        SMTP_PORT=587,
        SMTP_USERNAME="",
        SMTP_PASSWORD="",
        SMTP_FROM="stingray@localhost",
        SMTP_STARTTLS=True,
        SMTP_SSL=False,
    )
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(notifications, name, value)


# --- email_enabled / no-op cases --------------------------------------------

def test_disabled_when_host_unset(monkeypatch, captured):
    _configure(monkeypatch, SMTP_HOST="")
    assert notifications.email_enabled() is False
    notifications.send_email(["a@example.com"], "hi", "body")
    assert captured["server"] is None  # no connection attempted


def test_empty_recipients_noop(monkeypatch, captured):
    _configure(monkeypatch)
    notifications.send_email([], "hi", "body")
    notifications.send_email(["", None], "hi", "body")
    assert captured["server"] is None


# --- STARTTLS / SSL branching -----------------------------------------------

def test_ssl_path_uses_ssl_and_no_starttls(monkeypatch, captured):
    _configure(monkeypatch, SMTP_SSL=True, SMTP_STARTTLS=True, SMTP_PORT=465)
    notifications.send_email(["a@example.com"], "hi", "body")
    srv = captured["server"]
    assert srv.kind == "ssl"
    assert isinstance(srv.init_context, ssl.SSLContext)
    assert srv.starttls_context == "unset"  # starttls never called
    assert len(srv.sent) == 1


def test_starttls_path(monkeypatch, captured):
    _configure(monkeypatch, SMTP_SSL=False, SMTP_STARTTLS=True)
    notifications.send_email(["a@example.com"], "hi", "body")
    srv = captured["server"]
    assert srv.kind == "plain"
    assert isinstance(srv.starttls_context, ssl.SSLContext)  # context passed
    assert len(srv.sent) == 1


def test_plain_no_starttls(monkeypatch, captured):
    _configure(monkeypatch, SMTP_SSL=False, SMTP_STARTTLS=False)
    notifications.send_email(["a@example.com"], "hi", "body")
    srv = captured["server"]
    assert srv.kind == "plain"
    assert srv.starttls_context == "unset"


def test_login_skipped_without_password(monkeypatch, captured):
    _configure(monkeypatch, SMTP_USERNAME="user", SMTP_PASSWORD="")
    notifications.send_email(["a@example.com"], "hi", "body")
    assert captured["server"].logged_in is None


def test_login_used_with_credentials(monkeypatch, captured):
    _configure(monkeypatch, SMTP_USERNAME="user", SMTP_PASSWORD="pw")
    notifications.send_email(["a@example.com"], "hi", "body")
    assert captured["server"].logged_in == ("user", "pw")


# --- header sanitization -----------------------------------------------------

def test_subject_crlf_sanitized(monkeypatch, captured):
    _configure(monkeypatch)
    notifications.send_email(
        ["a@example.com"], "hi\r\nBcc: evil@example.com", "body"
    )
    msg = captured["server"].sent[0]
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"]
    assert msg["Subject"] == "hi Bcc: evil@example.com"


# --- never-raise: low level --------------------------------------------------

def test_send_email_swallows_connect_error(monkeypatch):
    _configure(monkeypatch)

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(notifications.smtplib, "SMTP", boom)
    # Must not raise.
    notifications.send_email(["a@example.com"], "hi", "body")


# --- never-raise: high level -------------------------------------------------

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeBackground:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


def test_notify_assignment_self_assignment_noop():
    bg = _FakeBackground()
    actor = _Obj(id=1, display_name="A", email="a@example.com")
    ticket = _Obj(id=1, title="t", type="bug", priority="low", status="open")
    notifications.notify_assignment(bg, ticket, actor, actor)
    assert bg.tasks == []


def test_notify_assignment_none_assignee_noop():
    bg = _FakeBackground()
    actor = _Obj(id=1, display_name="A", email="a@example.com")
    ticket = _Obj(id=1, title="t", type="bug", priority="low", status="open")
    notifications.notify_assignment(bg, ticket, None, actor)
    assert bg.tasks == []


def test_notify_assignment_queues_task():
    bg = _FakeBackground()
    actor = _Obj(id=1, display_name="A", email="a@example.com")
    assignee = _Obj(id=2, display_name="B", email="b@example.com")
    ticket = _Obj(id=5, title="t", type="bug", priority="low", status="open")
    notifications.notify_assignment(bg, ticket, assignee, actor)
    assert len(bg.tasks) == 1
    fn, args, _ = bg.tasks[0]
    assert fn is notifications.send_email
    assert args[0] == ["b@example.com"]


def test_notify_assignment_swallows_attribute_error():
    """A broken model object must not propagate out of the helper."""
    bg = _FakeBackground()
    actor = _Obj(id=1, display_name="A", email="a@example.com")
    assignee = _Obj(id=2)  # missing display_name/email -> AttributeError
    ticket = _Obj(id=5)  # missing fields too
    # Must not raise; nothing queued because it failed before add_task.
    notifications.notify_assignment(bg, ticket, assignee, actor)


def test_notify_new_ticket_admins_swallows_db_error():
    bg = _FakeBackground()

    class _BadDB:
        def query(self, *a, **k):
            raise RuntimeError("db gone")

    actor = _Obj(id=1, display_name="A", email="a@example.com")
    ticket = _Obj(id=5, title="t", type="bug", priority="low", status="open")
    # Must not raise.
    notifications.notify_new_ticket_admins(bg, _BadDB(), ticket, actor)
    assert bg.tasks == []


def test_notify_new_ticket_admins_excludes_actor_and_queues():
    bg = _FakeBackground()
    actor = _Obj(id=1, display_name="A", email="a@example.com")
    other = _Obj(id=2, display_name="B", email="b@example.com")
    ticket = _Obj(id=5, title="t", type="bug", priority="low", status="open")

    class _DB:
        def query(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def all(self):
            return [actor, other]

    notifications.notify_new_ticket_admins(bg, _DB(), ticket, actor)
    assert len(bg.tasks) == 1
    _, args, _ = bg.tasks[0]
    assert args[0] == ["b@example.com"]  # actor excluded


def test_env_int_bad_value_falls_back():
    assert notifications._env_int("DEFINITELY_NOT_SET_XYZ", 587) == 587
    os.environ["TMP_BAD_PORT"] = "notanumber"
    try:
        assert notifications._env_int("TMP_BAD_PORT", 587) == 587
    finally:
        del os.environ["TMP_BAD_PORT"]
