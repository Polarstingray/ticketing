# External agents

Stingray's own resolver has no privileged backdoor — it is an ordinary API client
that happens to be trusted with a few control tags. This document is about giving
*someone else's* agent the same standing: a third-party worker that watches the
ticket stream, does the work its own way, and reports back.

The whole integration is four steps — **register → subscribe → claim → report** —
and it needs no server restart, no env edit, and no admin role for the worker.

## The trust model

Some tags are not cosmetic. `claude:*` drives a workflow state machine, `repo:` /
`rev:` / `branch:` aim automation at a checkout, and `dangerous` / `fix` /
`delegate` are safety gates. If any client could set them, it could hijack the
automation, so they are gated in [`backend/control_tags.py`](../backend/control_tags.py).

Before external agents there were only two ways past that gate: be an admin, or be
listed in the server's `RESOLVER_BOT_USER_ID`. Both are far too much authority for
a third party, and the second means editing server env for every new worker.

The `agent` **API-key scope** is the third way. A scope is a narrow capability
carried by a *key* rather than by its owner, so it can be granted without touching
the worker's role, and revoking the key revokes the capability. Only an admin may
grant one.

| Tag | `cli` scope | `agent` scope | Why |
|---|---|---|---|
| `parent:<id>` | ✗ | **✓** | Links a sub-task to the ticket that spawned it — the agent's own bookkeeping. |
| `review-by:<id>` | ✗ | **✓** | Records who finished work is handed back to. |
| `repo:<name>` | ✓ | ✗ | *Aims* our resolver at a checkout. An external agent brings its own. |
| `rev:<sha>`, `branch:<name>` | ✓ | ✗ | Same: pins which code a fix lands on. |
| `claude:*` | ✗ | ✗ | Drives our resolver's phase machine. |
| `dangerous`, `fix`, `delegate` | ✗ | ✗ | Safety gates and autonomous fan-out. |

`agent` is deliberately **narrower than `cli`, not a superset**. The worst a forged
routing tag does is misfile a handoff; a forged aiming tag points automation at code
of the caller's choosing, which is the exact attack the gate exists to stop.

Everything else about the worker's authority is unchanged: it is an ordinary member,
so it sees and modifies only tickets it created or is assigned to.

## 1. Register

An admin creates a user for the worker and mints it a scoped key. Do this from
**Profile → API keys** in the UI, or over the API:

```bash
# Create the worker's user (admin session or admin key).
curl -s -X POST "$BASE/users" -H "X-API-Key: $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"username": "triage-agent", "display_name": "Triage agent",
       "email": "triage@example.com", "password": "…", "role": "member"}'

# Mint it an agent-scoped key. Only an admin may set `scopes`.
curl -s -X POST "$BASE/users/$USER_ID/api-keys" -H "X-API-Key: $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name": "triage-agent prod", "scopes": ["agent"]}'
```

The plaintext key is returned **exactly once**. Everything below authenticates with
`X-API-Key: $AGENT_KEY`.

### Announce liveness

`POST /agents/heartbeat` records who you are and when you were last seen, so an
operator can tell a stopped worker from a busy one. It is an upsert keyed on the
caller's own user id — post it on a timer (once a sweep, or every few minutes).

```bash
curl -s -X POST "$BASE/agents/heartbeat" -H "X-API-Key: $AGENT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"label": "prod-us-east", "name": "triage", "agent": "custom",
       "model": "gpt-x", "effective_config": {"poll_seconds": 30}}'
```

`effective_config` is a free-form dict for whatever non-secret settings you want an
operator to see. **Never put a secret in it** — admins read it back.

Registered workers show up under **Resolvers → External agents** in the admin UI,
with a freshness dot and a last-seen time. `GET /agents` (admin-only) is the same
list over the API; it covers every worker that has ever heartbeated, ours and
third-party alike.

> `POST /resolvers/heartbeat` is the older, resolver-only twin of this endpoint. It
> writes the same row but requires the caller to be one of *our* resolver bots.
> External agents should use `/agents/heartbeat`.

## 2. Subscribe

Rather than polling, tail the event log over Server-Sent Events. The connection is
long-lived and the client dials out, so a worker behind NAT needs no open port.

```bash
curl -sN -H "X-API-Key: $AGENT_KEY" "$BASE/events/stream?last_event_id=$LAST_SEEN"
```

You see only events on tickets you created or are assigned to. A fresh connection
starts at the head and does **not** replay history — persist the `id:` of the last
event you processed and pass it back as `last_event_id` (or the standard
`Last-Event-ID` header, which `EventSource` resends for you) to cover a gap. See
[`api_guide.md`](../api_guide.md#get-eventsstream) for the event shapes.

## 3. Claim

Work reaches an agent by **assignment**: a human (or another agent) assigns the
ticket, and `ticket.assigned` arrives on the stream. Because a member may modify
only tickets they created or are assigned to, that assignment *is* the claim — an
agent cannot reach out and grab an arbitrary ticket, which is what keeps one
worker from hoovering up another's queue.

Once assigned, mark that you have started, and use `parent:` / `review-by:` to
record how the work relates to other work:

```bash
curl -s -X PATCH "$BASE/tickets/$TICKET_ID" -H "X-API-Key: $AGENT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"status": "in_progress", "tags": ["triage", "parent:41", "review-by:7"]}'
```

`tags` is a full replacement, and reserved tags you are **not** allowed to set are
preserved rather than dropped — so a `claude:*` tag already on the ticket survives
your PATCH instead of failing it. Sending a reserved tag your key doesn't cover is
a `422`.

## 4. Report

Post one record per phase of work to
[`POST /tickets/{id}/agent-runs`](../api_guide.md), the same surface our resolver
uses. It carries the model, token usage, cost and outcome, and the app rolls it
into the ticket's cost timeline and the spend rollups.

```bash
curl -s -X POST "$BASE/tickets/$TICKET_ID/agent-runs" -H "X-API-Key: $AGENT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"agent": "claude", "phase": "implement", "model": "…",
       "input_tokens": 12000, "output_tokens": 900, "cost_usd": 0.11,
       "status": "succeeded",
       "started_at": "2026-08-27T10:00:00Z", "finished_at": "2026-08-27T10:04:00Z"}'
```

Authorization is `can_modify_ticket`, so being the assignee (step 3) is exactly
what lets you post runs against the ticket — no extra scope needed.

One current limitation: `agent` and `phase` are closed enumerations
(`claude | opencode | review-api | critique-api` and
`plan | implement | review | plan-critique`), inherited from when the resolver was
the only writer. A third-party worker has to map its own runtime and phase names
onto those values until the enumerations are widened.

Comments are the other reporting surface and have no such constraint: a plain
`POST /tickets/{id}/comments` is how a worker explains itself to a human.

## Checklist

- [ ] Admin created the worker's user and minted a key with `scopes: ["agent"]`.
- [ ] Worker heartbeats `POST /agents/heartbeat` on a timer, with no secrets in `effective_config`.
- [ ] Worker tails `GET /events/stream` and persists the last event id it handled.
- [ ] Worker acts only on tickets assigned to it.
- [ ] Worker posts an agent run per phase, and a comment when a human needs to read something.
- [ ] The key is stored as a secret and rotated by revoking it — that alone removes the scope.
