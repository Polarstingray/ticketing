"""SQLAlchemy engine, session factory, and the get_db dependency."""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

# SQLite file location. In Docker this is mounted on a named volume at /data.
DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "data", "stingray.db"))
os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},  # required for SQLite + FastAPI threadpool
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """Per-connection SQLite tuning, applied on every new connection.

    SQLite allows a single writer at a time, and the webhook dispatcher
    (``dispatcher.py``) writes on its own connection concurrently with the
    request path. Two pragmas make that survivable:

    * **WAL** lets readers proceed while a write is in flight, instead of the
      rollback journal's whole-database lock. It is a persistent property of the
      database file, so re-setting it per connection is a cheap no-op.
    * **busy_timeout** makes a connection that finds the write lock held *wait*
      for it rather than raising ``database is locked`` immediately. Without it
      the dispatcher and a concurrent request would trade spurious failures.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def get_db():
    """FastAPI dependency that yields a request-scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
