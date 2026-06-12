"""Stand up a throwaway Stingray backend for an eval run.

We point the backend at a temp SQLite DB, seed an *admin* user (the simulated human
who approves plans) and a *bot* user (the resolver's identity), mint an API key for
each, then run the real `uvicorn` server as a subprocess. Everything is faithful to
production — the resolver talks to it over HTTP exactly as it does to the real app.

Seeding is done directly through the backend ORM (imported here) rather than over HTTP,
so we control the bot's user id and can hand its key to the resolver before the server
is even up.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class BackendHandle:
    base_url: str          # e.g. http://127.0.0.1:8123  (NO /api — that's a frontend proxy)
    admin_key: str         # acts as the human (create tickets, approve, reassign)
    bot_key: str           # the resolver's key
    bot_user_id: int
    admin_user_id: int
    db_path: Path
    _proc: subprocess.Popen

    def stop(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def _seed(db_path: Path) -> tuple[str, int, str, int]:
    """Create schema + admin/bot users and keys in `db_path`. Returns
    (admin_key, admin_id, bot_key, bot_id). Imports backend modules with DATABASE_PATH
    pointed at our temp file, so create_all lands in the right place."""
    os.environ["DATABASE_PATH"] = str(db_path)
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))

    # Import only after DATABASE_PATH is set (database.py reads it at import time).
    from database import Base, engine, SessionLocal  # type: ignore
    from migrations import run_migrations            # type: ignore
    from auth import generate_api_key, hash_api_key, hash_password  # type: ignore
    from models import ApiKey, User, UserRole        # type: ignore

    Base.metadata.create_all(bind=engine)
    run_migrations(engine)

    db = SessionLocal()
    try:
        def make_user(username: str, role: str) -> tuple[int, str]:
            user = User(username=username, display_name=username,
                        email=f"{username}@eval.local", role=role,
                        hashed_password=hash_password("eval-password"))
            db.add(user)
            db.flush()
            raw = generate_api_key()
            db.add(ApiKey(user_id=user.id, name="eval", key_prefix=raw[:11],
                          key_hash=hash_api_key(raw)))
            return user.id, raw

        admin_id, admin_key = make_user("eval-admin", UserRole.admin.value)
        bot_id, bot_key = make_user("eval-bot", UserRole.member.value)
        db.commit()
        return admin_key, admin_id, bot_key, bot_id
    finally:
        db.close()


def start_backend(db_path: Path, python: str | None = None) -> BackendHandle:
    """Seed `db_path` and launch uvicorn against it; block until /health responds."""
    admin_key, admin_id, bot_key, bot_id = _seed(db_path)
    port = _free_port()
    python = python or sys.executable

    env = dict(os.environ)
    env["DATABASE_PATH"] = str(db_path)
    # The backend recognizes the resolver bot by id (control_tags.RESOLVER_BOT_USER_ID,
    # read from ITS env) to let it manage reserved claude:*/repo: tags. Without this the
    # bot's state-machine PATCHes are rejected 422 and nothing ever plans.
    env["RESOLVER_BOT_USER_ID"] = str(bot_id)
    # Keep the dev defaults (the startup security checks only bite when APP_ENV looks
    # like prod or COOKIE_SECURE=true); we want neither here.
    env.pop("APP_ENV", None)
    env.pop("COOKIE_SECURE", None)

    proc = subprocess.Popen(
        [python, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("backend process exited before becoming healthy")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.25)
    else:
        proc.terminate()
        raise RuntimeError(f"backend did not become healthy at {base_url}")

    return BackendHandle(base_url=base_url, admin_key=admin_key, bot_key=bot_key,
                         bot_user_id=bot_id, admin_user_id=admin_id, db_path=db_path,
                         _proc=proc)
