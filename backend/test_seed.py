"""seed_resolver_bot provisions the optional bot + bootstrap file (gated)."""
import json
import os
import stat
import uuid

from database import DATABASE_PATH, SessionLocal
from models import ApiKey, User
from seed import seed_resolver_bot


def _bootstrap_path() -> str:
    return os.path.join(os.path.dirname(DATABASE_PATH), "resolver-bootstrap.json")


def _run(monkeypatch, **env) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    db = SessionLocal()
    try:
        seed_resolver_bot(db)
    finally:
        db.close()


def test_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv("SEED_RESOLVER_BOT", raising=False)
    name = f"bot_{uuid.uuid4().hex[:8]}"
    _run(monkeypatch, RESOLVER_BOT_USERNAME=name)
    db = SessionLocal()
    try:
        assert db.query(User).filter(User.username == name).first() is None
    finally:
        db.close()


def test_seeds_bot_and_writes_bootstrap(client, monkeypatch):
    name = f"bot_{uuid.uuid4().hex[:8]}"
    _run(monkeypatch, SEED_RESOLVER_BOT="true", RESOLVER_BOT_USERNAME=name)

    db = SessionLocal()
    try:
        bot = db.query(User).filter(User.username == name).first()
        assert bot is not None
        assert bot.is_resolver_bot is True
        assert bot.role == "member"  # least privilege
        assert db.query(ApiKey).filter(ApiKey.user_id == bot.id).count() == 1
        bot_id = bot.id
    finally:
        db.close()

    path = _bootstrap_path()
    assert os.path.exists(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(open(path).read())
    assert data["user_id"] == bot_id
    assert data["username"] == name
    assert data["api_key"].startswith("sk_")


def test_idempotent(client, monkeypatch):
    name = f"bot_{uuid.uuid4().hex[:8]}"
    _run(monkeypatch, SEED_RESOLVER_BOT="true", RESOLVER_BOT_USERNAME=name)
    _run(monkeypatch, SEED_RESOLVER_BOT="true", RESOLVER_BOT_USERNAME=name)  # no raise
    db = SessionLocal()
    try:
        assert db.query(User).filter(User.username == name).count() == 1
    finally:
        db.close()
