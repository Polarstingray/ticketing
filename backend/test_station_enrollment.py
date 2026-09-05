"""Station enrolment: one bot's credentials, without an admin key on the host.

The shape under test is an asymmetry. Minting is gated on `require_recent_admin`,
which an API key cannot satisfy at all — that gate is the feature, not a
formality, because the whole point is that no program holds the authority to
create bots. Redeeming has no auth, because a station has nothing yet, so the
token has to behave like a credential in every other respect.
"""
import uuid
from datetime import timedelta

import pytest

from database import SessionLocal
from models import StationEnrollment, User, utcnow


def H(key: str) -> dict:
    return {"X-API-Key": key}


def _admin_client(new_client):
    """A fresh cookie session as the seeded admin — the only way to mint."""
    c = new_client()
    r = c.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    return c


def _name() -> str:
    return f"enrolled-{uuid.uuid4().hex[:8]}"


def _mint(c, username: str, **body):
    return c.post("/station-enrollments", json={"username": username, **body})


# --- minting ----------------------------------------------------------------

def test_mint_returns_the_token_exactly_once(new_client):
    c = _admin_client(new_client)
    name = _name()
    r = _mint(c, name)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert token.startswith("st_")

    listed = c.get("/station-enrollments").json()
    mine = next(e for e in listed if e["username"] == name)
    # The listing carries a prefix for recognition and nothing usable.
    assert mine["token_prefix"] == token[:11]
    assert "token" not in mine
    assert token not in repr(listed)


def test_an_api_key_can_never_mint(client, admin_key):
    """`require_recent_admin` reads session age, which a key does not have.

    This is the gate the feature rests on: if a program could mint an enrolment
    token, it could create bots, and holding an admin key on the workstation
    would be no worse than holding this.
    """
    r = client.post("/station-enrollments", json={"username": _name()},
                    headers=H(admin_key))
    assert r.status_code == 401
    assert r.json()["detail"] == "reauth_required"


def test_minting_for_an_existing_username_is_refused(new_client):
    c = _admin_client(new_client)
    r = _mint(c, "admin")
    assert r.status_code == 400


def test_ttl_is_bounded(new_client):
    c = _admin_client(new_client)
    assert _mint(c, _name(), expires_in_seconds=5).status_code == 422
    assert _mint(c, _name(), expires_in_seconds=99999999).status_code == 422


# --- redeeming --------------------------------------------------------------

def test_redeem_creates_the_bot_and_a_working_key(client, new_client):
    c = _admin_client(new_client)
    name = _name()
    token = _mint(c, name).json()["token"]

    r = client.post("/station-enrollments/redeem",
                    json={"token": token, "station": "ubvm.home.lab"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == name

    # The key works, and the identity it belongs to is a resolver bot — which
    # is what lets it set reserved control tags without being an admin.
    who = client.get("/auth/me", headers=H(body["api_key"]))
    assert who.status_code == 200, who.text
    assert who.json()["id"] == body["user_id"]

    db = SessionLocal()
    try:
        bot = db.query(User).filter(User.id == body["user_id"]).one()
        assert bot.is_resolver_bot is True
        assert bot.role == "member"
    finally:
        db.close()

    listed = c.get("/station-enrollments").json()
    mine = next(e for e in listed if e["username"] == name)
    assert mine["redeemed_at"] is not None
    assert mine["redeemed_user_id"] == body["user_id"]
    # Reported at redeem, so an admin sees where a bot was enrolled without
    # waiting for its first heartbeat.
    assert mine["station"] == "ubvm.home.lab"


def test_a_token_is_single_use(client, new_client):
    c = _admin_client(new_client)
    token = _mint(c, _name()).json()["token"]
    assert client.post("/station-enrollments/redeem", json={"token": token}).status_code == 200
    again = client.post("/station-enrollments/redeem", json={"token": token})
    assert again.status_code == 404


def test_an_expired_token_is_refused(client, new_client):
    c = _admin_client(new_client)
    name = _name()
    token = _mint(c, name).json()["token"]

    db = SessionLocal()
    try:
        row = db.query(StationEnrollment).filter(StationEnrollment.username == name).one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    assert client.post("/station-enrollments/redeem", json={"token": token}).status_code == 404


@pytest.mark.parametrize("token", ["st_nope", "", "not-a-token"])
def test_every_rejection_looks_identical(client, new_client, token):
    """Otherwise the endpoint is an oracle for which tokens ever existed."""
    c = _admin_client(new_client)
    name = _name()
    spent = _mint(c, name).json()["token"]
    client.post("/station-enrollments/redeem", json={"token": spent})

    used = client.post("/station-enrollments/redeem", json={"token": spent})
    bogus = client.post("/station-enrollments/redeem", json={"token": token or "x"})
    assert used.status_code == bogus.status_code == 404
    assert used.json()["detail"] == bogus.json()["detail"]


def test_a_rejected_redeem_creates_nothing(client, new_client):
    before = _user_count()
    assert client.post("/station-enrollments/redeem",
                       json={"token": "st_definitely-not-real"}).status_code == 404
    assert _user_count() == before


def _user_count() -> int:
    db = SessionLocal()
    try:
        return db.query(User).count()
    finally:
        db.close()


# --- revoking ---------------------------------------------------------------

def test_revoking_a_pending_token_makes_it_unusable(client, new_client):
    c = _admin_client(new_client)
    name = _name()
    minted = _mint(c, name).json()
    assert c.delete(f"/station-enrollments/{minted['id']}").status_code == 204
    assert client.post("/station-enrollments/redeem",
                       json={"token": minted["token"]}).status_code == 404


def test_a_redeemed_enrolment_is_kept_as_a_record(client, new_client):
    """The bot it created still exists; the row is how that identity came to be.

    Revoking access to a live bot means revoking its API key, which is a
    different operation with a different blast radius.
    """
    c = _admin_client(new_client)
    minted = _mint(c, _name()).json()
    client.post("/station-enrollments/redeem", json={"token": minted["token"]})
    r = c.delete(f"/station-enrollments/{minted['id']}")
    assert r.status_code == 409
    assert "API key" in r.json()["detail"]


def test_listing_requires_admin(client, make_user):
    member = make_user()
    assert client.get("/station-enrollments", headers=H(member.key)).status_code == 403
