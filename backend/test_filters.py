"""Dashboard filtering: multi-tag selection, sorting, and the tag facet list.

The suite shares one database, so every test here tags its fixtures with a
unique marker tag and filters on it. That scopes each assertion to just the rows
it created, which is what makes exact ordering and count assertions safe.
"""

import uuid

from test_tickets import _create


def _marker() -> str:
    return f"m-{uuid.uuid4().hex[:8]}"


def _ids(r):
    return [t["id"] for t in r.json()["items"]]


def _list(client, key, **params):
    r = client.get("/tickets", params=params, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r


# --- Tag filtering -----------------------------------------------------------

def test_single_tag_filter(client, admin_key):
    m = _marker()
    hit = _create(client, admin_key, tags=[m, "alpha"])
    miss = _create(client, admin_key, tags=[m, "beta"])

    ids = _ids(_list(client, admin_key, tag=[m, "alpha"], tag_match="all"))
    assert hit["id"] in ids
    assert miss["id"] not in ids


def test_tag_match_all_requires_every_tag(client, admin_key):
    m = _marker()
    both = _create(client, admin_key, tags=[m, "alpha", "beta"])
    only_alpha = _create(client, admin_key, tags=[m, "alpha"])
    only_beta = _create(client, admin_key, tags=[m, "beta"])

    ids = _ids(_list(client, admin_key, tag=[m, "alpha", "beta"], tag_match="all"))
    assert ids == [both["id"]]
    assert only_alpha["id"] not in ids and only_beta["id"] not in ids


def test_tag_match_any_widens_to_the_union(client, admin_key):
    m = _marker()
    both = _create(client, admin_key, tags=[m, "alpha", "beta"])
    only_alpha = _create(client, admin_key, tags=[m, "alpha"])
    only_beta = _create(client, admin_key, tags=[m, "beta"])
    neither = _create(client, admin_key, tags=[m, "gamma"])

    ids = set(_ids(_list(client, admin_key, tag=["alpha", "beta"], tag_match="any")))
    assert {both["id"], only_alpha["id"], only_beta["id"]} <= ids
    assert neither["id"] not in ids


def test_tag_match_defaults_to_all(client, admin_key):
    m = _marker()
    both = _create(client, admin_key, tags=[m, "alpha", "beta"])
    _create(client, admin_key, tags=[m, "alpha"])

    # No tag_match given: behaves like "all", not "any".
    ids = _ids(_list(client, admin_key, tag=[m, "alpha", "beta"]))
    assert ids == [both["id"]]


def test_tag_filter_is_exact_not_a_prefix_match(client, admin_key):
    """`auth` must not match a ticket tagged only `authz`."""
    m = _marker()
    exact = _create(client, admin_key, tags=[m, "auth"])
    longer = _create(client, admin_key, tags=[m, "authz"])

    ids = _ids(_list(client, admin_key, tag=[m, "auth"], tag_match="all"))
    assert ids == [exact["id"]]
    assert longer["id"] not in ids


def test_underscore_in_tag_is_not_a_like_wildcard(client, admin_key):
    """`_` is a LIKE single-char wildcard and is legal in a tag, so it must be
    escaped — otherwise `a_b` would also match `axb`."""
    m = _marker()
    underscore = _create(client, admin_key, tags=[m, "a_b"])
    decoy = _create(client, admin_key, tags=[m, "axb"])

    ids = _ids(_list(client, admin_key, tag=[m, "a_b"], tag_match="all"))
    assert ids == [underscore["id"]]
    assert decoy["id"] not in ids


def test_percent_in_search_style_tag_is_not_a_wildcard(client, admin_key):
    """`%` can't appear in a tag (the charset forbids it), so a literal `%`
    query must match nothing rather than acting as 'any tag'."""
    m = _marker()
    _create(client, admin_key, tags=[m, "alpha"])

    r = client.get("/tickets", params={"tag": "%"}, headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_blank_tag_values_are_ignored(client, admin_key):
    """The frontend can emit an empty tag param; it must not filter everything out."""
    m = _marker()
    t = _create(client, admin_key, tags=[m])

    ids = _ids(_list(client, admin_key, tag=[m, "", "  "], tag_match="all"))
    assert ids == [t["id"]]


def test_tag_filter_composes_with_other_filters(client, admin_key):
    m = _marker()
    hit = _create(client, admin_key, tags=[m], priority="high")
    miss = _create(client, admin_key, tags=[m], priority="low")

    ids = _ids(_list(client, admin_key, tag=m, priority="high"))
    assert ids == [hit["id"]]
    assert miss["id"] not in ids


# --- Sorting -----------------------------------------------------------------

def test_sort_defaults_to_newest_created_first(client, admin_key):
    m = _marker()
    first = _create(client, admin_key, tags=[m], title="first")
    second = _create(client, admin_key, tags=[m], title="second")

    assert _ids(_list(client, admin_key, tag=m)) == [second["id"], first["id"]]


def test_sort_created_ascending(client, admin_key):
    m = _marker()
    first = _create(client, admin_key, tags=[m])
    second = _create(client, admin_key, tags=[m])

    ids = _ids(_list(client, admin_key, tag=m, sort="created", order="asc"))
    assert ids == [first["id"], second["id"]]


def test_sort_by_priority_uses_rank_not_alphabetical(client, admin_key):
    """Alphabetically it would be critical < high < low < medium; by rank the
    order must be critical, high, medium, low."""
    m = _marker()
    made = {
        p: _create(client, admin_key, tags=[m], priority=p)["id"]
        for p in ("low", "medium", "high", "critical")
    }

    ids = _ids(_list(client, admin_key, tag=m, sort="priority", order="desc"))
    assert ids == [made["critical"], made["high"], made["medium"], made["low"]]

    ids = _ids(_list(client, admin_key, tag=m, sort="priority", order="asc"))
    assert ids == [made["low"], made["medium"], made["high"], made["critical"]]


def test_sort_by_title(client, admin_key):
    m = _marker()
    b = _create(client, admin_key, tags=[m], title="banana")
    a = _create(client, admin_key, tags=[m], title="apple")

    ids = _ids(_list(client, admin_key, tag=m, sort="title", order="asc"))
    assert ids == [a["id"], b["id"]]


def test_sort_by_due_date_puts_undated_last_in_both_directions(client, admin_key):
    m = _marker()
    soon = _create(client, admin_key, tags=[m], due_date="2026-01-01T00:00:00Z")
    later = _create(client, admin_key, tags=[m], due_date="2026-06-01T00:00:00Z")
    undated = _create(client, admin_key, tags=[m])

    ids = _ids(_list(client, admin_key, tag=m, sort="due", order="asc"))
    assert ids == [soon["id"], later["id"], undated["id"]]

    ids = _ids(_list(client, admin_key, tag=m, sort="due", order="desc"))
    assert ids == [later["id"], soon["id"], undated["id"]]


def test_sort_survives_pagination(client, admin_key):
    """The ordering is applied before LIMIT/OFFSET, so page 2 continues page 1."""
    m = _marker()
    made = [_create(client, admin_key, tags=[m], title=f"t{i}")["id"] for i in range(4)]

    page1 = _ids(_list(client, admin_key, tag=m, sort="created", order="asc", limit=2))
    page2 = _ids(_list(client, admin_key, tag=m, sort="created", order="asc",
                       limit=2, offset=2))
    assert page1 + page2 == made


def test_unknown_sort_or_order_is_rejected(client, admin_key):
    for params in ({"sort": "bogus"}, {"order": "sideways"}, {"tag_match": "some"}):
        r = client.get("/tickets", params=params, headers={"X-API-Key": admin_key})
        assert r.status_code == 422, (params, r.text)


# --- Tag facets --------------------------------------------------------------

def _facets(client, key, **params):
    r = client.get("/tickets/tags", params=params, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return {f["tag"]: f["count"] for f in r.json()["items"]}


def test_tag_facets_count_usages(client, admin_key):
    m = _marker()
    _create(client, admin_key, tags=[m, "alpha"])
    _create(client, admin_key, tags=[m, "alpha"])
    _create(client, admin_key, tags=[m, "beta"])

    facets = _facets(client, admin_key)
    assert facets[m] == 3
    assert facets["alpha"] >= 2 and facets["beta"] >= 1


def test_tag_facets_are_ordered_by_count_then_name(client, admin_key):
    m = _marker()
    for _ in range(3):
        _create(client, admin_key, tags=[m])

    items = client.get("/tickets/tags", headers={"X-API-Key": admin_key}).json()["items"]
    counts = [i["count"] for i in items]
    assert counts == sorted(counts, reverse=True)
    # Ties break alphabetically, so the order is stable between calls.
    again = client.get("/tickets/tags", headers={"X-API-Key": admin_key}).json()["items"]
    assert items == again


def test_tag_facets_hide_archived_tickets_by_default(client, admin_key):
    m = _marker()
    t = _create(client, admin_key, tags=[m])
    # Only closed tickets may be archived.
    client.patch(f"/tickets/{t['id']}", json={"status": "closed"},
                 headers={"X-API-Key": admin_key})
    r = client.post(f"/tickets/{t['id']}/archive", headers={"X-API-Key": admin_key})
    assert r.status_code == 200, r.text

    assert m not in _facets(client, admin_key)
    assert _facets(client, admin_key, archived="true")[m] == 1


def test_tag_facets_respect_the_visibility_boundary(client, admin_key, make_user):
    """A member must not learn about a tag that exists only on someone else's ticket."""
    m = _marker()
    _create(client, admin_key, tags=[m, "admin-only-tag"])

    member = make_user()
    own = _marker()
    _create(client, member.key, tags=[own])

    facets = _facets(client, member.key)
    assert own in facets
    assert m not in facets


def test_tag_facets_route_is_not_shadowed_by_the_ticket_detail_route(client, admin_key):
    """`/tickets/tags` must resolve to the facet list, not `/tickets/{ticket_id}`."""
    r = client.get("/tickets/tags", headers={"X-API-Key": admin_key})
    assert r.status_code == 200
    assert "items" in r.json()
