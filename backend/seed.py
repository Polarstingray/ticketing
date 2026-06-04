"""First-run seeding: create an initial admin user if the table is empty."""
import os

from sqlalchemy.orm import Session

from auth import generate_api_key, hash_password
from models import User, UserRole


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
        api_key=generate_api_key(),
    )
    db.add(admin)
    db.commit()
    print(f"[seed] Created initial admin user '{username}' with API key {admin.api_key}")
