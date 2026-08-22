"""First-run seeding: create an initial admin user if the table is empty."""
import json
import os
import stat

from sqlalchemy.orm import Session

from auth import generate_api_key, hash_api_key, hash_password
from database import DATABASE_PATH
from models import ApiKey, User, UserRole


def seed_admin(db: Session) -> None:
    """Create an admin from ADMIN_* env vars when no users exist yet."""
    if db.query(User).count() > 0:
        return

    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin")
    email = os.environ.get("ADMIN_EMAIL", "admin@example.com")

    admin = User(
        username=username,
        display_name=username,
        email=email,
        role=UserRole.admin.value,
        hashed_password=hash_password(password),
    )
    db.add(admin)
    db.flush()  # assign admin.id

    raw_key = generate_api_key()
    db.add(
        ApiKey(
            user_id=admin.id,
            name="default",
            key_prefix=raw_key[:11],
            key_hash=hash_api_key(raw_key),
        )
    )
    db.commit()
    print(
        f"[seed] Created initial admin user '{username}' with API key {raw_key}\n"
        f"[seed] This key is shown only once — store it now."
    )


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def create_resolver_bot(
    db: Session,
    username: str,
    *,
    display_name: str | None = None,
    email: str | None = None,
) -> tuple[User, str]:
    """Create a least-privilege ``member`` user flagged ``is_resolver_bot=True``
    (so it may set the reserved control tags without being an admin) and mint its
    first API key. Returns ``(bot, raw_key)``; the raw key is shown only here.

    Does NOT commit — the caller owns the transaction (the seed path also writes a
    bootstrap file; the admin API path returns the key over HTTP). Shared so both
    entry points create identical bots.
    """
    bot = User(
        username=username,
        display_name=display_name or username,
        email=email or f"{username}@localhost",
        role=UserRole.member.value,
        is_resolver_bot=True,
        hashed_password=hash_password(generate_api_key()),  # random; bot logs in by API key
    )
    db.add(bot)
    db.flush()  # assign bot.id

    raw_key = generate_api_key()
    db.add(
        ApiKey(
            user_id=bot.id,
            name="resolver",
            key_prefix=raw_key[:11],
            key_hash=hash_api_key(raw_key),
        )
    )
    return bot, raw_key


def seed_resolver_bot(db: Session) -> None:
    """Provision the optional resolver bot when ``SEED_RESOLVER_BOT`` is truthy.

    Creates a least-privilege ``member`` user flagged ``is_resolver_bot=True`` (so
    it may set the reserved control tags without being an admin), mints one API
    key, and writes the bot's id + raw key to a one-time bootstrap file next to
    the database (mode 600). ``install.sh`` reads that file to populate
    ``resolver/.env`` automatically, so the operator never hand-creates the bot or
    syncs ``RESOLVER_BOT_USER_ID``. Idempotent: skips if the bot already exists.
    """
    if not _truthy(os.environ.get("SEED_RESOLVER_BOT")):
        return

    username = os.environ.get("RESOLVER_BOT_USERNAME", "claude-bot")
    if db.query(User).filter(User.username == username).first():
        return  # already provisioned on an earlier boot

    bot, raw_key = create_resolver_bot(
        db, username, email=os.environ.get("RESOLVER_BOT_EMAIL")
    )
    db.commit()

    bootstrap = {"user_id": bot.id, "username": username, "api_key": raw_key}
    path = os.path.join(os.path.dirname(DATABASE_PATH), "resolver-bootstrap.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(bootstrap, fh)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600 — it holds a live key
    except OSError as exc:  # non-fatal: the key is also printed below
        print(f"[seed] WARNING: could not write resolver bootstrap file: {exc}")

    print(
        f"[seed] Created resolver bot '{username}' (id={bot.id}) with API key {raw_key}\n"
        f"[seed] Wrote {path} for install.sh; this key is shown only once."
    )


DIGEST_KEY_NAME = "digest"


def seed_digest_admin_key(db: Session) -> None:
    """Mint a dedicated admin API key for the daily digest when ``SEED_DIGEST_BOT``
    is truthy.

    The digest surveys the *whole* backlog, and the API shows non-admins only their
    own tickets — so unlike the resolver it cannot run on a least-privilege bot user
    and needs an admin's key. Rather than creating a second admin (which would hand
    a scheduled job its own privileged login), this mints an extra key named
    ``digest`` for the admin that already exists, so it can be revoked on its own
    from Profile → API keys without touching the admin's primary key.

    The raw key is written next to the database as ``digest-bootstrap.json`` (mode
    600); ``install.sh`` reads it to fill in ``DIGEST_ADMIN_KEY`` in
    ``resolver/.env``. Idempotent: skips if the admin already has a ``digest`` key.
    """
    if not _truthy(os.environ.get("SEED_DIGEST_BOT")):
        return

    username = os.environ.get("ADMIN_USERNAME", "admin")
    admin = (
        db.query(User)
        .filter(User.username == username, User.role == UserRole.admin.value)
        .first()
    )
    if admin is None:  # fall back to any admin (username may have been changed)
        admin = (
            db.query(User)
            .filter(User.role == UserRole.admin.value)
            .order_by(User.id)
            .first()
        )
    if admin is None:
        print("[seed] SEED_DIGEST_BOT is set but no admin user exists — skipping.")
        return

    existing = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == admin.id, ApiKey.name == DIGEST_KEY_NAME)
        .first()
    )
    if existing is not None:
        return  # already minted on an earlier boot

    raw_key = generate_api_key()
    db.add(
        ApiKey(
            user_id=admin.id,
            name=DIGEST_KEY_NAME,
            key_prefix=raw_key[:11],
            key_hash=hash_api_key(raw_key),
        )
    )
    db.commit()

    path = os.path.join(os.path.dirname(DATABASE_PATH), "digest-bootstrap.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"user_id": admin.id, "username": admin.username, "api_key": raw_key}, fh)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600 — it holds a live admin key
    except OSError as exc:  # non-fatal: the key is also printed below
        print(f"[seed] WARNING: could not write digest bootstrap file: {exc}")

    print(
        f"[seed] Minted digest admin key for '{admin.username}' (id={admin.id}): {raw_key}\n"
        f"[seed] Wrote {path} for install.sh; this key is shown only once."
    )
