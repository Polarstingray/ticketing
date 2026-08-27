"""Populate a database with illustrative demo data for screenshots / the hosted
demo / a screen-recorded walkthrough.

This is NOT the production first-run seed (that's ``seed.py`` — one admin, no
tickets). This script paints a *lived-in* instance: a handful of users, a spread
of tickets across every status/priority, and — the centerpiece — a code-review
ticket the AI resolver actually worked, with a per-phase agent-run timeline
(plan → implement → review), token usage, cost, and a delegated parent→child
fan-out so the cost-rollup UI has something real to show.

The numbers are representative of real Claude / opencode runs but are hand-set so
the demo is deterministic and costs nothing to produce.

Usage (point at a throwaway DB so you never touch real data):

    DATABASE_PATH=data/demo.db python -m seed_demo          # fresh demo DB
    DATABASE_PATH=data/demo.db python -m seed_demo --force  # wipe & re-seed

Login for the walkthrough: admin / demopass123 (override via ADMIN_*).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from auth import generate_api_key, hash_api_key, hash_password
from database import SessionLocal, engine
from models import (
    Activity,
    AgentRun,
    AgentRunStatus,
    ApiKey,
    Base,
    ChatConversation,
    ChatMessage,
    ChatRole,
    Comment,
    Ticket,
    TicketPriority,
    TicketStatus,
    TicketType,
    User,
    UserRole,
)

# A fixed "now" so every run produces identical, sensibly-ordered timestamps.
NOW = datetime(2026, 7, 15, 17, 30, 0)


def _ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


def _make_user(db: Session, *, username, display_name, email, role, password,
               is_bot=False) -> User:
    u = User(
        username=username,
        display_name=display_name,
        email=email,
        role=role,
        is_resolver_bot=is_bot,
        hashed_password=hash_password(password),
    )
    db.add(u)
    db.flush()
    return u


def _mint_key(db: Session, user: User, name: str) -> str:
    raw = generate_api_key()
    db.add(ApiKey(user_id=user.id, name=name, key_prefix=raw[:11],
                  key_hash=hash_api_key(raw)))
    return raw


def _activity(db, ticket, actor, action, detail, when):
    db.add(Activity(ticket_id=ticket.id, actor_id=actor.id if actor else None,
                    action=action, detail=detail, created_at=when))


def _run(db, ticket, *, agent, phase, model, in_tok, out_tok, cache_r, cache_w,
         cost, started, dur_s, status=AgentRunStatus.succeeded.value, log_tail=""):
    db.add(AgentRun(
        ticket_id=ticket.id, agent=agent, phase=phase, model=model,
        input_tokens=in_tok, output_tokens=out_tok,
        cache_read_tokens=cache_r, cache_write_tokens=cache_w, cost_usd=cost,
        status=status, log_tail=log_tail,
        started_at=started, finished_at=started + timedelta(seconds=dur_s),
        created_at=started + timedelta(seconds=dur_s),
    ))


# A realistic failed-run transcript tail. Already redacted, exactly as the
# resolver would send it — the «redacted» markers are the point, not noise.
DEMO_LOG_TAIL = """\
[opencode] loading model claude-sonnet-5
[opencode] POST https://gateway.internal/v1/chat/completions
[opencode] Authorization: Bearer «redacted»
[opencode] applying plan step 2/4: guard plan paths
Traceback (most recent call last):
  File "/work/resolver-wt/resolve_tickets.py", line 1841, in do_implement
    plan = find_approved_plan(client, ticket)
  File "/work/resolver-wt/resolve_tickets.py", line 1702, in find_approved_plan
    root = Path(entry["path"]).resolve()
KeyError: 'path'
[opencode] phase failed after 48s (exit 1)
[opencode] no changes written to the worktree
"""


def seed_demo(db: Session) -> None:
    admin_pw = os.environ.get("ADMIN_PASSWORD", "demopass123")
    admin = _make_user(db, username=os.environ.get("ADMIN_USERNAME", "admin"),
                       display_name="Ada Lovelace", email="admin@example.com",
                       role=UserRole.admin.value, password=admin_pw)
    admin_key = _mint_key(db, admin, "default")

    priya = _make_user(db, username="priya", display_name="Priya Natarajan",
                       email="priya@example.com", role=UserRole.member.value,
                       password="demopass123")
    marco = _make_user(db, username="marco", display_name="Marco Reyes",
                       email="marco@example.com", role=UserRole.member.value,
                       password="demopass123")

    # The resolver identity — a least-privilege member flagged is_resolver_bot.
    bot = _make_user(db, username="claude-bot", display_name="Claude (resolver)",
                     email="claude-bot@localhost", role=UserRole.member.value,
                     password=generate_api_key(), is_bot=True)
    bot_key = _mint_key(db, bot, "resolver")

    # ---- Background tickets: a realistic spread of the board --------------------
    def ticket(**kw):
        t = Ticket(**kw)
        db.add(t)
        db.flush()
        return t

    t_login = ticket(
        type=TicketType.task.value, title="Login form rejects valid emails with a '+' alias",
        description="Users with Gmail '+' aliases (e.g. me+stingray@gmail.com) "
                    "can't sign in — the client-side regex is too strict.",
        status=TicketStatus.in_review.value, priority=TicketPriority.high.value,
        created_by=priya.id, assigned_to=marco.id, tags=["frontend", "auth"],
        created_at=_ago(days=3, hours=2), updated_at=_ago(hours=6))
    _activity(db, t_login, priya, "created", None, _ago(days=3, hours=2))
    _activity(db, t_login, admin, "assigned", {"to": marco.id, "name": marco.display_name},
              _ago(days=3, hours=1))
    _activity(db, t_login, marco, "status_changed",
              {"from": "open", "to": "in_review"}, _ago(hours=6))

    ticket(
        type=TicketType.task.value, title="Add CSV export to the ticket list",
        description="Let admins export the current filtered view as CSV for "
                    "reporting. Should respect the active status/assignee filters.",
        status=TicketStatus.open.value, priority=TicketPriority.medium.value,
        created_by=marco.id, tags=["frontend", "backend", "reporting"],
        created_at=_ago(days=2, hours=5), updated_at=_ago(days=2, hours=5))

    ticket(
        type=TicketType.task.value, title="Upgrade to SQLAlchemy 2.0 typing style",
        description="Migrate the models to the 2.0 Mapped[]/mapped_column API for "
                    "better type-checking. Low risk, incremental.",
        status=TicketStatus.open.value, priority=TicketPriority.low.value,
        created_by=admin.id, tags=["backend", "tech-debt"],
        created_at=_ago(days=5), updated_at=_ago(days=5))

    ticket(
        type=TicketType.task.value, title="Notification badge count lags after marking all read",
        description="The unread badge keeps its old number until a full reload. "
                    "Likely a stale query cache after the bulk mark-read call.",
        status=TicketStatus.resolved.value, priority=TicketPriority.medium.value,
        created_by=priya.id, assigned_to=priya.id, tags=["frontend"],
        created_at=_ago(days=6), updated_at=_ago(days=1, hours=3))

    # ---- CENTERPIECE: a code-review ticket the resolver actually worked --------
    hero = ticket(
        type=TicketType.code_review.value,
        title="Review: batch the activity-feed queries (fix N+1)",
        description="The ticket detail page loads each activity's actor with a "
                    "separate query (classic N+1). Batch them with a single IN "
                    "load and add a covering index. Please review the change.",
        status=TicketStatus.resolved.value, priority=TicketPriority.high.value,
        created_by=priya.id, assigned_to=bot.id,
        tags=["backend", "performance", "repo:ticketing", "claude:done"],
        code_blocks=[{
            "filename": "backend/routers/tickets.py", "language": "python",
            "line_start": 245, "line_end": 268,
            "content": (
                "activities = (\n"
                "    db.query(Activity)\n"
                "    .filter(Activity.ticket_id == ticket_id)\n"
                "    .order_by(Activity.created_at.asc())\n"
                "    .all()\n"
                ")\n"
                "# N+1: one extra SELECT per row to resolve the actor.\n"
                "for a in activities:\n"
                "    a.actor  # lazy-loaded here\n"
            ),
        }],
        created_at=_ago(days=1, hours=8), updated_at=_ago(days=1, hours=6))

    # Timeline: created → picked up by the bot → plan/implement/review → resolved.
    plan_start = _ago(days=1, hours=7, minutes=40)
    impl_start = _ago(days=1, hours=7, minutes=32)
    review_start = _ago(days=1, hours=7, minutes=18)

    _activity(db, hero, priya, "created", None, _ago(days=1, hours=8))
    _activity(db, hero, admin, "assigned", {"to": bot.id, "name": bot.display_name},
              _ago(days=1, hours=7, minutes=50))
    _activity(db, hero, bot, "tags_changed", {"added": ["claude:planning"]},
              _ago(days=1, hours=7, minutes=41))
    _activity(db, hero, bot, "tags_changed",
              {"added": ["claude:implementing"], "removed": ["claude:planning"]},
              impl_start)
    _activity(db, hero, bot, "commented", None, review_start)
    _activity(db, hero, bot, "tags_changed",
              {"added": ["claude:done"], "removed": ["claude:implementing"]},
              _ago(days=1, hours=7, minutes=6))
    _activity(db, hero, bot, "status_changed",
              {"from": "open", "to": "resolved"}, _ago(days=1, hours=6))

    db.add(Comment(
        ticket_id=hero.id, author=bot.id,
        body=("Batched the actor load into a single `selectinload` and added a "
              "`(ticket_id, created_at)` index — activity queries drop from N+1 "
              "to 2. Opened PR #51 with the change and a regression test.\n\n"
              "Verified: `pytest backend/test_tickets.py -k activity` green."),
        created_at=review_start))

    # Per-phase agent runs (the auditable, costed timeline).
    _run(db, hero, agent="claude", phase="plan",
         model="claude-opus-4-8", in_tok=18_420, out_tok=1_960,
         cache_r=12_800, cache_w=9_400, cost=0.0731,
         started=plan_start, dur_s=95)
    _run(db, hero, agent="claude", phase="implement",
         model="claude-opus-4-8", in_tok=41_030, out_tok=6_240,
         cache_r=38_200, cache_w=15_100, cost=0.2184,
         started=impl_start, dur_s=760)
    _run(db, hero, agent="review-api", phase="review",
         model="claude-sonnet-5", in_tok=9_870, out_tok=1_310,
         cache_r=0, cache_w=0, cost=0.0492,
         started=review_start, dur_s=40)

    # ---- Delegated fan-out: a parent that decomposed into a sub-task ------------
    parent = ticket(
        type=TicketType.task.value,
        title="Harden the resolver's git-worktree isolation",
        description="Audit and fix the ways an agent run could escape its worktree "
                    "(absolute paths in plans, symlink traversal). Decompose as "
                    "needed and delegate sub-tasks.",
        status=TicketStatus.in_review.value, priority=TicketPriority.high.value,
        created_by=admin.id, assigned_to=bot.id,
        tags=["resolver", "security", "repo:ticketing", "delegate",
              "claude:implementing"],
        created_at=_ago(days=1, hours=2), updated_at=_ago(hours=3))
    _activity(db, parent, admin, "created", None, _ago(days=1, hours=2))
    _activity(db, parent, admin, "assigned",
              {"to": bot.id, "name": bot.display_name}, _ago(days=1, hours=2))

    p_plan = _ago(days=1, hours=1, minutes=50)
    _run(db, parent, agent="claude", phase="plan",
         model="claude-opus-4-8", in_tok=22_100, out_tok=3_480,
         cache_r=14_500, cache_w=11_200, cost=0.0968,
         started=p_plan, dur_s=140)

    child = ticket(
        type=TicketType.code_review.value,
        title="Reject absolute paths in resolver plan file lists",
        description="Sub-task of #{}: the implement phase must refuse plan entries "
                    "that resolve outside the worktree root.".format(parent.id),
        status=TicketStatus.resolved.value, priority=TicketPriority.high.value,
        created_by=bot.id, assigned_to=bot.id,
        tags=["resolver", "security", "repo:ticketing",
              "parent:{}".format(parent.id), "claude:done"],
        created_at=_ago(hours=5), updated_at=_ago(hours=3))
    _activity(db, child, bot, "created", None, _ago(hours=5))
    _activity(db, child, bot, "status_changed",
              {"from": "open", "to": "resolved"}, _ago(hours=3))
    db.add(Comment(
        ticket_id=child.id, author=bot.id,
        body="Added a `_within_worktree()` guard that rejects any plan path "
             "resolving outside the checkout. PR #52.",
        created_at=_ago(hours=3, minutes=20)))

    # A first implement attempt that failed, carrying the tail of its transcript.
    # This is what phase 4 exists for: without it the timeline can say a phase
    # failed but never what it said on the way out, and the transcript itself
    # lives only on the machine the resolver runs on. Note the redaction — the
    # resolver scrubs every registered credential before the tail leaves.
    _run(db, child, agent="opencode", phase="implement",
         model="claude-sonnet-5", in_tok=9_140, out_tok=310,
         cache_r=0, cache_w=0, cost=0.0288,
         started=_ago(hours=4, minutes=52), dur_s=48,
         status=AgentRunStatus.failed.value,
         log_tail=DEMO_LOG_TAIL)

    c_impl = _ago(hours=4, minutes=30)
    c_review = _ago(hours=3, minutes=40)
    _run(db, child, agent="opencode", phase="implement",
         model="claude-sonnet-5", in_tok=15_600, out_tok=2_410,
         cache_r=8_900, cache_w=6_300, cost=0.0642,
         started=c_impl, dur_s=420)
    _run(db, child, agent="review-api", phase="review",
         model="claude-sonnet-5", in_tok=7_240, out_tok=980,
         cache_r=0, cache_w=0, cost=0.0331,
         started=c_review, dur_s=35)

    # ---- A chat thread that answers the question the tail exists for ----------
    # Seeded rather than left to a live model so the hosted demo shows the
    # feature without needing CHAT_API_* configured. The assistant's turn carries
    # the same `meta` shape a real turn stores, so the tool disclosure and the
    # proposed-action card both render from it.
    # Owned by the admin the demo walkthrough logs in as. Conversations are
    # strictly per-owner — there is no admin override, deliberately, since a
    # thread quotes ticket content — so a thread seeded onto anyone else would be
    # invisible in the demo. Ada can also *view* this bot-owned ticket, which a
    # member could not: an anchor the owner cannot read makes every turn a 404.
    asked = _ago(hours=2, minutes=50)
    thread = ChatConversation(
        user_id=admin.id, ticket_id=child.id,
        title="Why did the first implement run fail?",
        created_at=asked, updated_at=asked + timedelta(seconds=12))
    db.add(thread)
    db.flush()  # need thread.id for the messages
    db.add(ChatMessage(
        conversation_id=thread.id, role=ChatRole.user.value,
        content="Why did the first implement run fail?",
        meta={"ticket_id": child.id, "context_chars": 4_820},
        created_at=asked))
    db.add(ChatMessage(
        conversation_id=thread.id, role=ChatRole.assistant.value,
        content=(
            "The first `implement` run failed with a `KeyError: 'path'` in "
            "`find_approved_plan()` — it read `entry[\"path\"]` from a plan entry "
            "that didn't have one, so the phase aborted after 48s without "
            "writing anything to the worktree.\n\n"
            "The retry 22 minutes later succeeded, and the change that landed "
            "(`_within_worktree()`) is unrelated to the crash — so the missing "
            "`path` key is still unguarded. Worth a ticket."
        ),
        model="claude-sonnet-5", input_tokens=6_240, output_tokens=310,
        cost_usd=0.0071,
        meta={
            "ticket_id": child.id,
            "context_chars": 4_820,
            "tool_calls": [
                {"name": "get_agent_runs", "args": {"ticket_id": child.id},
                 "summary": "1.1k chars", "chars": 1_136},
            ],
            "proposed_actions": [{
                "kind": "create_ticket",
                "payload": {
                    "type": "task",
                    "title": "find_approved_plan crashes on a plan entry with no `path`",
                    "description": (
                        "The first implement run on #{} died with KeyError: 'path'. "
                        "The retry succeeded for unrelated reasons, so the missing-key "
                        "case is still unguarded."
                    ).format(child.id),
                    "priority": "medium",
                    "tags": ["resolver"],
                },
                "rationale": "The crash is still unguarded; the retry passed by luck.",
            }],
        },
        created_at=asked + timedelta(seconds=12)))

    db.commit()

    print("[seed_demo] Seeded demo data:")
    print(f"  admin login    : {admin.username} / {admin_pw}")
    print(f"  admin API key  : {admin_key}")
    print(f"  resolver bot   : id={bot.id}  API key {bot_key}")
    print(f"  hero ticket    : #{hero.id} (code_review, 3 agent runs)")
    print(f"  delegation     : parent #{parent.id} -> child #{child.id}")
    print(f"  chat thread    : #{thread.id} on ticket #{child.id} "
          f"(owner {admin.username}), showing a failed run explained")


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed illustrative demo data.")
    ap.add_argument("--force", action="store_true",
                    help="wipe existing tickets/users and re-seed")
    args = ap.parse_args()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = db.query(User).count()
        if existing and not args.force:
            print("[seed_demo] Database already has data; refusing to seed. "
                  "Point DATABASE_PATH at a fresh DB or pass --force.",
                  file=sys.stderr)
            return 1
        if args.force:
            # Order matters for FK integrity on the child tables.
            for model in (ChatMessage, ChatConversation, AgentRun, Comment,
                          Activity, Ticket, ApiKey, User):
                db.query(model).delete()
            db.commit()
        seed_demo(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
