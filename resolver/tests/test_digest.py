"""The digest agent: config, bucketing, rendering, and the filing guard.

The part that must be exactly right is that the checklist is derived from the
query results and nothing else — the model never emits a ticket number — and that
a digest files at most once per day per name. No network, no model calls.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import digest as dg
from conftest import FakeClient

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
DAY = "2026-08-21"


def iso(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def ticket(tid, **over):
    base = {
        "id": tid,
        "title": f"Ticket {tid}",
        "status": "open",
        "priority": "medium",
        "assigned_to": 1,
        "tags": [],
        "due_date": None,
        "created_at": iso(hours=2),
        "updated_at": iso(hours=2),
    }
    base.update(over)
    return base


@pytest.fixture
def ctx():
    return dg.Ctx(now=NOW,
                  window_cutoff=NOW - timedelta(hours=24),
                  stale_cutoff=NOW - timedelta(days=7))


class DigestClient(FakeClient):
    """FakeClient's iter_tickets only understands `tag=`; the digest also filters
    by status/priority server-side, and needs list_users/whoami."""

    def iter_tickets(self, **filters):
        for t in self._tickets.values():
            tags = filters.get("tag")
            if tags and not set(tags if isinstance(tags, list) else [tags]) <= set(t.get("tags") or []):
                continue
            if "status" in filters and t.get("status") != filters["status"]:
                continue
            yield t

    def list_users(self):
        return [{"id": 1, "display_name": "admin", "username": "admin"}]

    def whoami(self):
        return {"id": 1, "username": "admin", "role": "admin"}


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(
        logs_dir=tmp_path,
        digest_admin_key="", digest_api_url="", digest_api_key="", digest_api_model="",
        review_api_url="", review_api_key="", review_api_model="",
        stingray_url="http://localhost:3000/api", stingray_max_retries=1,
    )


# --- config ------------------------------------------------------------------

MINIMAL = """
[[digest]]
name = "daily"
"""


def write(tmp_path, text):
    p = tmp_path / "digests.toml"
    p.write_text(text)
    return p


def test_a_minimal_block_gets_the_documented_defaults(tmp_path):
    d, = dg.load_digests(write(tmp_path, MINIMAL))
    assert d.name == "daily"
    assert d.max_tickets == 80 and d.window_hours == 24 and d.stale_days == 7
    assert d.sections == list(dg.DEFAULT_SECTIONS)


def test_every_field_round_trips(tmp_path):
    d, = dg.load_digests(write(tmp_path, """
[[digest]]
name = "repo"
title = "T {date}"
query = "tag=repo:x&status=open"
assign_to = 7
priority = "high"
tags = ["ops"]
window_hours = 168
stale_days = 14
max_tickets = 5
exclude_tags = ["noise"]
sections = ["stale", "unassigned"]
prompt = "  focus on ops  "
schedule = "weekly"
"""))
    assert (d.title, d.assign_to, d.priority, d.tags) == ("T {date}", 7, "high", ["ops"])
    assert (d.window_hours, d.stale_days, d.max_tickets) == (168, 14, 5)
    assert d.exclude_tags == ["noise"] and d.sections == ["stale", "unassigned"]
    assert d.prompt == "focus on ops"
    assert d.filters() == {"tag": ["repo:x"], "status": "open"}


@pytest.mark.parametrize("body, needle", [
    ('[[digest]]\nname = "Daily"\n', "slug"),
    ('[[digest]]\nname = "d"\nquery = "statuss=open"\n', "statuss"),
    ('[[digest]]\nname = "d"\nsections = ["nope"]\n', "nope"),
    ('[[digest]]\nname = "d"\npriority = "urgent"\n', "priority"),
    ('[[digest]]\nname = "d"\nmax_tickets = 0\n', "max_tickets"),
    ('[[digest]]\nname = "d"\nwidnow_hours = 3\n', "widnow_hours"),
    ('[[digest]]\nname = "d"\n\n[[digest]]\nname = "d"\n', "duplicate"),
])
def test_bad_config_is_rejected_at_load_time(tmp_path, body, needle):
    """Every one of these would otherwise fail mid-run, or — worse for the typo'd
    query param — succeed against the wrong set of tickets."""
    with pytest.raises(SystemExit) as e:
        dg.load_digests(write(tmp_path, body))
    assert needle in str(e.value)


def test_a_missing_config_names_the_example_file(tmp_path):
    with pytest.raises(SystemExit, match="digests.example.toml"):
        dg.load_digests(tmp_path / "nope.toml")


def test_repeated_tag_params_collapse_into_a_list():
    assert dg.parse_query("?tag=a&tag=b&status=open") == {
        "tag": ["a", "b"], "status": "open"}


# --- bucketing ---------------------------------------------------------------

def test_each_ticket_lands_in_its_section(ctx):
    tickets = [
        ticket(1, due_date=iso(days=2)),                         # overdue
        ticket(2, priority="critical"),                          # high-priority
        ticket(3, tags=["resolver:awaiting-fix"]),               # awaiting-fix
        ticket(4, assigned_to=None),                             # unassigned
        ticket(5, updated_at=iso(days=30)),                      # stale
        ticket(6, created_at=iso(hours=1)),                      # new
        ticket(7, status="resolved", updated_at=iso(hours=3)),   # recently-resolved
    ]
    sections, leftovers = dg.bucket(tickets, list(dg.DEFAULT_SECTIONS), ctx)
    got = {name: [t["id"] for t in ts] for name, _, ts in sections}
    assert got == {"overdue": [1], "high-priority": [2], "awaiting-fix": [3],
                   "unassigned": [4], "stale": [5], "new": [6],
                   "recently-resolved": [7]}
    assert leftovers == []


def test_a_ticket_appears_exactly_once_even_when_it_matches_everything(ctx):
    """First-match-wins is what keeps the checklist a to-do list rather than the
    same ticket repeated under four headings."""
    everything = ticket(1, priority="critical", assigned_to=None,
                        due_date=iso(days=1), updated_at=iso(days=30),
                        tags=["resolver:awaiting-fix"])
    sections, leftovers = dg.bucket([everything], list(dg.DEFAULT_SECTIONS), ctx)
    assert [name for name, _, _ in sections] == ["overdue"]
    assert leftovers == []


def test_section_order_comes_from_the_digest_not_the_registry(ctx):
    t = ticket(1, priority="critical", assigned_to=None)
    sections, _ = dg.bucket([t], ["unassigned", "high-priority"], ctx)
    assert [name for name, _, _ in sections] == ["unassigned"]


def test_unmatched_tickets_become_leftovers_not_checklist_entries(ctx):
    sections, leftovers = dg.bucket([ticket(9)], ["overdue", "stale"], ctx)
    assert sections == [] and [t["id"] for t in leftovers] == [9]


def test_terminal_tickets_are_excluded_from_actionable_sections(ctx):
    """A closed ticket is not overdue, stale, or unassigned work."""
    closed = ticket(1, status="closed", assigned_to=None,
                    due_date=iso(days=5), updated_at=iso(days=40))
    sections, leftovers = dg.bucket(
        [closed], ["overdue", "unassigned", "stale"], ctx)
    assert sections == [] and leftovers == [closed]


def test_a_malformed_timestamp_does_not_take_down_the_run(ctx):
    sections, leftovers = dg.bucket(
        [ticket(1, updated_at="not-a-date", due_date="also-bad")],
        ["overdue", "stale"], ctx)
    assert sections == [] and len(leftovers) == 1


# --- collect -----------------------------------------------------------------

def test_collect_caps_and_reports_what_it_dropped(ctx):
    client = DigestClient(tickets=[ticket(i) for i in range(1, 11)])
    d = dg.Digest(name="d", max_tickets=4)
    kept, dropped = dg.collect(client, d, ctx)
    assert len(kept) == 4 and dropped == 6


def test_a_digest_never_surveys_its_own_past_reports(ctx):
    """Without the implicit `digest` exclusion, every digest would report on all
    the ones before it, and the backlog would look busier every day."""
    client = DigestClient(tickets=[ticket(1), ticket(2, tags=[dg.DIGEST_TAG])])
    kept, _ = dg.collect(client, dg.Digest(name="d"), ctx)
    assert [t["id"] for t in kept] == [1]


def test_exclude_tags_are_applied_client_side(ctx):
    client = DigestClient(tickets=[ticket(1), ticket(2, tags=["noise"])])
    kept, _ = dg.collect(client, dg.Digest(name="d", exclude_tags=["noise"]), ctx)
    assert [t["id"] for t in kept] == [1]


# --- rendering ---------------------------------------------------------------

def test_the_checklist_has_one_line_per_ticket_and_every_id(ctx):
    tickets = [ticket(i, priority="critical") for i in (11, 22, 33)]
    sections, leftovers = dg.bucket(tickets, ["high-priority"], ctx)
    body = dg.render(dg.Digest(name="d"), sections, leftovers, "", ctx, DAY,
                     {1: "admin"}, 0)
    assert body.count("- [ ] ") == 3
    for tid in (11, 22, 33):
        assert f"#{tid} " in body


def test_the_prose_is_optional_and_the_checklist_is_not(ctx):
    """A missing API key costs you a paragraph, never the report."""
    sections, leftovers = dg.bucket([ticket(1, priority="high")], ["high-priority"], ctx)
    body = dg.render(dg.Digest(name="d"), sections, leftovers, "", ctx, DAY, {}, 0)
    assert "- [ ] #1" in body and dg.DIGEST_MARKER in body


def test_the_cap_is_reported_in_the_body(ctx):
    body = dg.render(dg.Digest(name="d", max_tickets=2), [], [], "", ctx, DAY, {}, 7)
    assert "7 further ticket(s)" in body and "max_tickets = 2" in body


def test_an_empty_digest_says_so_instead_of_rendering_nothing(ctx):
    body = dg.render(dg.Digest(name="d"), [], [], "", ctx, DAY, {}, 0)
    assert "quiet day" in body


def test_titles_are_flattened_and_truncated():
    """Titles are user-authored and land in both the report and the prompt."""
    long = ticket(1, title="x" * 500)
    assert len(dg._title(long)) == dg.MAX_TITLE_CHARS
    assert dg._title(ticket(1, title="a\n  b")) == "a b"
    assert dg._title(ticket(1, title="")) == "(untitled)"


def test_the_scope_footer_records_the_query_that_produced_the_report(ctx):
    d = dg.Digest(name="daily", query="status=open&sort=priority")
    body = dg.render(d, [], [], "", ctx, DAY, {}, 0)
    assert "status=open&sort=priority" in body and "--name daily" in body


# --- the prompt --------------------------------------------------------------

def test_the_model_is_told_not_to_emit_ticket_numbers(ctx):
    """The checklist is derived from data; prose that also listed ids could
    contradict it."""
    sections, leftovers = dg.bucket([ticket(1)], ["new"], ctx)
    prompt = dg.build_prompt(dg.Digest(name="d"), sections, leftovers, ctx, {})
    assert "Do NOT list or cite ticket numbers" in prompt
    assert "untrusted user input" in prompt


def test_the_owners_extra_instructions_reach_the_prompt(ctx):
    prompt = dg.build_prompt(dg.Digest(name="d", prompt="mention flaky tests"),
                             [], [], ctx, {})
    assert "mention flaky tests" in prompt


def test_prose_config_falls_back_to_the_review_api(cfg):
    cfg.review_api_url, cfg.review_api_key, cfg.review_api_model = "u", "k", "m"
    assert dg.prose_config(cfg) == ("u", "k", "m")
    cfg.digest_api_model = "digest-model"
    assert dg.prose_config(cfg) == ("u", "k", "digest-model")


def test_no_endpoint_configured_yields_no_prose_and_no_error(cfg, tmp_path):
    assert dg.write_prose(cfg, "prompt", tmp_path / "l.log") == ("", {}, "")


# --- the run -----------------------------------------------------------------

def test_a_run_files_one_task_ticket_carrying_the_date_tag(cfg):
    client = DigestClient(tickets=[ticket(1, priority="critical")])
    d = dg.Digest(name="daily", assign_to=3, title="Daily — {date}")
    filed = dg.run_digest(cfg, client, d, now=NOW, names={})
    assert filed is not None
    body, = [c for c in client.created]
    assert body["type"] == "task"
    assert body["title"] == f"Daily — {DAY}"
    assert body["assigned_to"] == 3
    assert body["tags"] == [dg.DIGEST_TAG, "digest:daily", f"digest:daily:{DAY}"]
    assert "- [ ] #1" in body["description"]


def test_the_report_tags_are_all_free_tags(cfg):
    """The digest needs admin *read*; it must not also need authority to set a
    reserved tag, or a non-admin run would fail at the last step."""
    from stingray_client.tickets import is_reserved_tag
    tags = dg.Digest(name="daily", tags=["ops"]).report_tags(DAY)
    assert not [t for t in tags if is_reserved_tag(t)]


def test_a_second_run_on_the_same_day_files_nothing(cfg):
    client = DigestClient(tickets=[ticket(1)])
    d = dg.Digest(name="daily")
    assert dg.run_digest(cfg, client, d, now=NOW, names={}) is not None
    assert dg.run_digest(cfg, client, d, now=NOW, names={}) is None
    assert len(client.created) == 1


def test_force_overrides_the_once_a_day_guard(cfg):
    client = DigestClient(tickets=[ticket(1)])
    d = dg.Digest(name="daily")
    dg.run_digest(cfg, client, d, now=NOW, names={})
    assert dg.run_digest(cfg, client, d, now=NOW, force=True, names={}) is not None
    assert len(client.created) == 2


def test_two_digests_do_not_block_each_other(cfg):
    """The guard is keyed per name, so a weekly and a daily can share a day."""
    client = DigestClient(tickets=[ticket(1)])
    dg.run_digest(cfg, client, dg.Digest(name="daily"), now=NOW, names={})
    assert dg.run_digest(cfg, client, dg.Digest(name="weekly"), now=NOW,
                         names={}) is not None
    assert len(client.created) == 2


def test_dry_run_creates_nothing(cfg, capsys):
    client = DigestClient(tickets=[ticket(1, priority="high")])
    assert dg.run_digest(cfg, client, dg.Digest(name="daily"), now=NOW,
                         dry_run=True, names={}) is None
    assert client.created == []
    assert "- [ ] #1" in capsys.readouterr().out


def test_user_names_degrade_to_ids_rather_than_failing(cfg):
    class NoUsers(DigestClient):
        def list_users(self):
            raise RuntimeError("403 admin only")

    client = NoUsers(tickets=[ticket(1, priority="high")])
    dg.run_digest(cfg, client, dg.Digest(name="daily"), now=NOW)
    assert "@1" in client.created[0]["description"]


def test_closed_tickets_render_pre_ticked(ctx):
    """An unticked box beside a closed ticket reads as a to-do the reader has to
    check and dismiss; these sections report, they don't ask."""
    done = ticket(1, status="resolved", updated_at=iso(hours=3))
    sections, leftovers = dg.bucket([done], ["recently-resolved"], ctx)
    body = dg.render(dg.Digest(name="d"), sections, leftovers, "", ctx, DAY, {}, 0)
    assert "- [x] #1" in body and "- [ ]" not in body


def test_one_huge_section_cannot_swallow_the_report(ctx):
    """A backlog of 80 unassigned exercise stubs otherwise renders as a wall that
    buries the two overdue lines someone actually needed to see."""
    tickets = [ticket(i, assigned_to=None) for i in range(1, 41)]
    sections, leftovers = dg.bucket(tickets, ["unassigned"], ctx)
    d = dg.Digest(name="d", max_per_section=5)
    body = dg.render(d, sections, leftovers, "", ctx, DAY, {}, 0)
    assert body.count("- [ ] ") == 5
    assert "…and 35 more" in body
    assert "### Unassigned (40)" in body, "the heading must still show the true count"


def test_the_prompt_is_capped_the_same_way(ctx):
    tickets = [ticket(i, assigned_to=None) for i in range(1, 41)]
    sections, leftovers = dg.bucket(tickets, ["unassigned"], ctx)
    prompt = dg.build_prompt(dg.Digest(name="d", max_per_section=5),
                             sections, leftovers, ctx, {})
    assert prompt.count("[medium/open]") == 5
    assert "## Unassigned (40)" in prompt


# --- the statuses fan-out ----------------------------------------------------

def test_statuses_fans_the_query_out_and_merges(ctx):
    """The resolver parks reviewed tickets at in_review, so a digest pinned to
    status=open could never show an awaiting-fix section at all."""
    client = DigestClient(tickets=[
        ticket(1, status="open"),
        ticket(2, status="in_review", tags=["resolver:awaiting-fix"]),
        ticket(3, status="closed"),
    ])
    d = dg.Digest(name="d", statuses=["open", "in_review"])
    kept, _ = dg.collect(client, d, ctx)
    assert sorted(t["id"] for t in kept) == [1, 2]


def test_a_ticket_matching_two_queries_is_not_listed_twice(ctx):
    client = DigestClient(tickets=[ticket(1, status="open")])
    d = dg.Digest(name="d", statuses=["open", "open"])
    kept, _ = dg.collect(client, d, ctx)
    assert [t["id"] for t in kept] == [1]


def test_the_cap_keeps_the_urgent_tickets_across_a_fan_out(ctx):
    """Capping mid-fetch would let the first status exhaust the budget and drop
    every critical ticket sitting in the second."""
    client = DigestClient(tickets=[
        *[ticket(i, status="open", priority="low") for i in range(1, 6)],
        ticket(99, status="in_review", priority="critical"),
    ])
    d = dg.Digest(name="d", statuses=["open", "in_review"], max_tickets=2)
    kept, dropped = dg.collect(client, d, ctx)
    assert kept[0]["id"] == 99 and dropped == 4


def test_statuses_and_an_inline_status_are_rejected_together(tmp_path):
    with pytest.raises(SystemExit, match="not both"):
        dg.load_digests(write(tmp_path, '''
[[digest]]
name = "d"
query = "status=open"
statuses = ["open", "in_review"]
'''))


def test_an_unknown_status_is_rejected(tmp_path):
    with pytest.raises(SystemExit, match="in_reviewww"):
        dg.load_digests(write(tmp_path, '''
[[digest]]
name = "d"
statuses = ["in_reviewww"]
'''))


def test_stale_outranks_unassigned_by_default(ctx):
    """In a tracker where little is ever assigned, `unassigned` placed first
    swallows the backlog and every other heading goes empty."""
    rotting = ticket(1, assigned_to=None, updated_at=iso(days=70))
    sections, _ = dg.bucket([rotting], list(dg.DEFAULT_SECTIONS), ctx)
    assert [name for name, _, _ in sections] == ["stale"]
