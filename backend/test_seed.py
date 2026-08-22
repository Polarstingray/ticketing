"""seed_resolver_bot provisions the optional bot + bootstrap file (gated), and
seed_digest_admin_key mints the digest's admin key the same way."""
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


def test_digest_key_falls_back_to_lowest_id_admin(client, monkeypatch):
    """ADMIN_USERNAME naming nobody (renamed/deleted) picks the lowest-id admin,
    not a second admin created later, and never a non-admin."""
    _clear_digest_keys()
    db = SessionLocal()
    try:
        first_admin = (
            db.query(User)
            .filter(User.role == UserRole.admin.value)
            .order_by(User.id)
            .first()
        )
        assert first_admin is not None
        expected_id = first_admin.id
        # A later admin and a member, both of which the fallback must pass over.
        db.add_all(
            [
                User(
                    username=f"admin2_{uuid.uuid4().hex[:8]}",
                    display_name="second admin",
                    email=f"{uuid.uuid4().hex[:8]}@localhost",
                    role=UserRole.admin.value,
                    hashed_password="x",
                ),
                User(
                    username=f"member_{uuid.uuid4().hex[:8]}",
                    display_name="a member",
                    email=f"{uuid.uuid4().hex[:8]}@localhost",
                    role=UserRole.member.value,
                    hashed_password="x",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    _run_digest(
        monkeypatch, SEED_DIGEST_BOT="true", ADMIN_USERNAME="nobody-by-this-name"
    )

    db = SessionLocal()
    try:
        keys = db.query(ApiKey).filter(ApiKey.name == DIGEST_KEY_NAME).all()
        assert len(keys) == 1
        assert keys[0].user_id == expected_id
    finally:
        db.close()

    # A second run under yet another unresolvable name is still a no-op: the
    # fallback lands on the same admin, which already holds a digest key.
    _run_digest(monkeypatch, SEED_DIGEST_BOT="true", ADMIN_USERNAME="also-nobody")
    db = SessionLocal()
    try:
        assert db.query(ApiKey).filter(ApiKey.name == DIGEST_KEY_NAME).count() == 1
    finally:
        db.close()


def test_digest_key_survives_unwritable_bootstrap_file(client, monkeypatch, capsys):
    """If the bootstrap file can't be written the key is still minted, and the
    raw value is printed so the operator isn't locked out of it."""
    _clear_digest_keys()

    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("seed.open", _boom, raising=False)
    _run_digest(monkeypatch, SEED_DIGEST_BOT="true")

    db = SessionLocal()
    try:
        keys = db.query(ApiKey).filter(ApiKey.name == DIGEST_KEY_NAME).all()
        assert len(keys) == 1
        prefix = keys[0].key_prefix
    finally:
        db.close()

    assert not os.path.exists(_digest_bootstrap_path())
    out = capsys.readouterr().out
    assert "could not write digest bootstrap file" in out
    assert prefix in out  # the fallback really printed the usable key


def test_digest_key_skipped_when_no_admin(monkeypatch, capsys):
    """No admin at all logs and no-ops rather than raising during lifespan."""

    class _NoAdminDB:
        def query(self, *args, **kwargs):
            return self

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def first(self):
            return None

    monkeypatch.setenv("SEED_DIGEST_BOT", "true")
    seed_digest_admin_key(_NoAdminDB())
    assert "no admin user exists" in capsys.readouterr().out
