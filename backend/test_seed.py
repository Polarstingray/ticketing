"""seed_resolver_bot / seed_digest_admin_key provision the optional bot and the
digest's admin key, each with a one-time bootstrap file (both gated by env)."""
import json
import os
import stat
import uuid

from database import DATABASE_PATH, SessionLocal
from models import ApiKey, User, UserRole
from seed import DIGEST_KEY_NAME, seed_digest_admin_key, seed_resolver_bot


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


# --- digest admin key -----------------------------------------------------


def _digest_bootstrap_path() -> str:
    return os.path.join(os.path.dirname(DATABASE_PATH), "digest-bootstrap.json")


def _clear_digest_keys() -> None:
    """The whole suite shares one DB (and one seeded admin), so drop any digest
    key an earlier test minted to make each case below deterministic."""
    db = SessionLocal()
    try:
        db.query(ApiKey).filter(ApiKey.name == DIGEST_KEY_NAME).delete()
        db.commit()
    finally:
        db.close()
    if os.path.exists(_digest_bootstrap_path()):
        os.unlink(_digest_bootstrap_path())


def _run_digest(monkeypatch, **env) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    db = SessionLocal()
    try:
        seed_digest_admin_key(db)
    finally:
        db.close()


def test_digest_key_disabled_by_default(client, monkeypatch):
    _clear_digest_keys()
    monkeypatch.delenv("SEED_DIGEST_BOT", raising=False)
    _run_digest(monkeypatch)
    db = SessionLocal()
    try:
        assert db.query(ApiKey).filter(ApiKey.name == DIGEST_KEY_NAME).count() == 0
    finally:
        db.close()
    assert not os.path.exists(_digest_bootstrap_path())


def test_digest_key_minted_for_admin(client, monkeypatch):
    _clear_digest_keys()
    _run_digest(monkeypatch, SEED_DIGEST_BOT="true")

    db = SessionLocal()
    try:
        keys = db.query(ApiKey).filter(ApiKey.name == DIGEST_KEY_NAME).all()
        assert len(keys) == 1
        owner = db.query(User).filter(User.id == keys[0].user_id).first()
        # The digest surveys every ticket, so its key must be an admin's.
        assert owner.role == UserRole.admin.value
        owner_id = owner.id
        owner_name = owner.username
    finally:
        db.close()

    path = _digest_bootstrap_path()
    assert os.path.exists(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(open(path).read())
    assert data["user_id"] == owner_id
    assert data["username"] == owner_name
    assert data["api_key"].startswith("sk_")


def test_digest_key_idempotent(client, monkeypatch):
    _clear_digest_keys()
    _run_digest(monkeypatch, SEED_DIGEST_BOT="true")
    first = json.loads(open(_digest_bootstrap_path()).read())["api_key"]
    _run_digest(monkeypatch, SEED_DIGEST_BOT="true")  # no raise, no second key

    db = SessionLocal()
    try:
        assert db.query(ApiKey).filter(ApiKey.name == DIGEST_KEY_NAME).count() == 1
    finally:
        db.close()
    # The bootstrap file is left as written on the boot that minted the key.
    assert json.loads(open(_digest_bootstrap_path()).read())["api_key"] == first


def test_digest_key_authenticates_as_admin(client, monkeypatch):
    """The minted key really works against an admin-only endpoint."""
    _clear_digest_keys()
    _run_digest(monkeypatch, SEED_DIGEST_BOT="true")
    key = json.loads(open(_digest_bootstrap_path()).read())["api_key"]

    r = client.get("/auth/me", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"
