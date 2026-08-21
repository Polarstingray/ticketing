"""Saved dashboard views: CRUD plus the per-user ownership boundary.

The interesting cases are the negative ones — a saved view is trivially
personal, so the tests that matter are the ones proving one user cannot see or
touch another's.
"""

import uuid


def _name() -> str:
    return f"view-{uuid.uuid4().hex[:8]}"


def _create(client, key, name=None, query="status=open"):
    r = client.post("/saved-views", json={"name": name or _name(), "query": query},
                    headers={"X-API-Key": key})
    assert r.status_code == 201, r.text
    return r.json()


# --- CRUD --------------------------------------------------------------------

def test_create_and_list(client, make_user):
    user = make_user()
    view = _create(client, user.key, name="My open bugs",
                   query="status=open&tag=bug&sort=priority")

    assert view["name"] == "My open bugs"
    assert view["query"] == "status=open&tag=bug&sort=priority"

    r = client.get("/saved-views", headers={"X-API-Key": user.key})
    assert r.status_code == 200
    assert [v["id"] for v in r.json()] == [view["id"]]


def test_list_is_sorted_by_name(client, make_user):
    user = make_user()
    _create(client, user.key, name="zulu")
    _create(client, user.key, name="alpha")

    names = [v["name"] for v in client.get(
        "/saved-views", headers={"X-API-Key": user.key}).json()]
    assert names == ["alpha", "zulu"]


def test_rename_and_requery(client, make_user):
    user = make_user()
    view = _create(client, user.key, name="old", query="status=open")

    r = client.patch(f"/saved-views/{view['id']}",
                     json={"name": "new", "query": "status=closed"},
                     headers={"X-API-Key": user.key})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "new"
    assert r.json()["query"] == "status=closed"


def test_patch_is_partial(client, make_user):
    user = make_user()
    view = _create(client, user.key, name="keep-me", query="status=open")

    r = client.patch(f"/saved-views/{view['id']}", json={"query": "priority=high"},
                     headers={"X-API-Key": user.key})
    assert r.status_code == 200
    assert r.json()["name"] == "keep-me"
    assert r.json()["query"] == "priority=high"


def test_delete(client, make_user):
    user = make_user()
    view = _create(client, user.key)

    r = client.delete(f"/saved-views/{view['id']}", headers={"X-API-Key": user.key})
    assert r.status_code == 204
    assert client.get("/saved-views", headers={"X-API-Key": user.key}).json() == []


# --- Ownership ---------------------------------------------------------------

def test_views_are_private_to_their_owner(client, make_user):
    owner, other = make_user(), make_user()
    _create(client, owner.key, name="private")

    assert client.get("/saved-views", headers={"X-API-Key": other.key}).json() == []


def test_another_user_cannot_update_or_delete_a_view(client, make_user):
    owner, other = make_user(), make_user()
    view = _create(client, owner.key, name="private")

    # 404 rather than 403: a distinct 403 would confirm the id exists.
    r = client.patch(f"/saved-views/{view['id']}", json={"name": "hijacked"},
                     headers={"X-API-Key": other.key})
    assert r.status_code == 404
    assert client.delete(f"/saved-views/{view['id']}",
                         headers={"X-API-Key": other.key}).status_code == 404

    # And the view is untouched.
    still = client.get("/saved-views", headers={"X-API-Key": owner.key}).json()
    assert still[0]["name"] == "private"


def test_an_admin_does_not_see_other_users_views(client, admin_key, make_user):
    """These are personal, not moderated content — admin is not a superuser here."""
    member = make_user()
    _create(client, member.key, name="members-only")

    names = [v["name"] for v in client.get(
        "/saved-views", headers={"X-API-Key": admin_key}).json()]
    assert "members-only" not in names


def test_saved_views_require_authentication(client):
    assert client.get("/saved-views").status_code in (401, 403)


# --- Validation --------------------------------------------------------------

def test_duplicate_name_for_the_same_user_is_a_conflict(client, make_user):
    user = make_user()
    name = _name()
    _create(client, user.key, name=name)

    r = client.post("/saved-views", json={"name": name, "query": ""},
                    headers={"X-API-Key": user.key})
    assert r.status_code == 409


def test_two_users_may_use_the_same_view_name(client, make_user):
    a, b = make_user(), make_user()
    _create(client, a.key, name="Shared name")
    _create(client, b.key, name="Shared name")


def test_rename_onto_an_existing_name_is_a_conflict(client, make_user):
    user = make_user()
    _create(client, user.key, name="taken")
    other = _create(client, user.key, name="free")

    r = client.patch(f"/saved-views/{other['id']}", json={"name": "taken"},
                     headers={"X-API-Key": user.key})
    assert r.status_code == 409


def test_renaming_a_view_to_its_own_name_is_allowed(client, make_user):
    """The duplicate check must exclude the row being updated."""
    user = make_user()
    view = _create(client, user.key, name="stable")

    r = client.patch(f"/saved-views/{view['id']}",
                     json={"name": "stable", "query": "status=open"},
                     headers={"X-API-Key": user.key})
    assert r.status_code == 200


def test_blank_and_oversized_names_are_rejected(client, make_user):
    user = make_user()
    for bad in ("", "   ", "x" * 61, "has\nnewline"):
        r = client.post("/saved-views", json={"name": bad, "query": ""},
                        headers={"X-API-Key": user.key})
        assert r.status_code == 422, (bad, r.text)


def test_oversized_query_is_rejected(client, make_user):
    user = make_user()
    r = client.post("/saved-views", json={"name": _name(), "query": "a=" + "b" * 1000},
                    headers={"X-API-Key": user.key})
    assert r.status_code == 422


def test_a_leading_question_mark_is_stripped(client, make_user):
    """`location.search` includes the '?'; store the bare query string."""
    user = make_user()
    view = _create(client, user.key, query="?status=open&tag=bug")
    assert view["query"] == "status=open&tag=bug"


def test_query_defaults_to_empty(client, make_user):
    user = make_user()
    r = client.post("/saved-views", json={"name": _name()},
                    headers={"X-API-Key": user.key})
    assert r.status_code == 201
    assert r.json()["query"] == ""


def test_view_count_is_capped_per_user(client, make_user):
    from schemas import MAX_SAVED_VIEWS

    user = make_user()
    for i in range(MAX_SAVED_VIEWS):
        _create(client, user.key, name=f"v{i:03d}")

    r = client.post("/saved-views", json={"name": "one-too-many", "query": ""},
                    headers={"X-API-Key": user.key})
    assert r.status_code == 422
    assert "max" in r.json()["detail"].lower()

    # The cap is per user, so a different account is unaffected.
    _create(client, make_user().key, name="v000")
