#!/usr/bin/env python3
"""Push wakeup: hold the SSE stream and poke the resolver unit on assignment.

The timer path polls: worst-case pickup latency is the full timer interval. This
daemon closes that gap to about a second by holding an outbound connection to
`GET /api/events/stream` and starting `stingray-resolver.service` when a ticket
lands in the bot's queue.

**Why the resolver dials out.** The documented topology is Stingray on a server
and the resolver on a dev station, which is behind NAT and cannot receive an
inbound webhook. Inverting the direction needs no open port and no tunnel.

**Why it pokes systemd instead of calling `sweep()`.** systemd already
serializes starts of a unit and merges a start job into one that is already
queued, so delegating gets no-overlap and burst debouncing for free — and
`resolve_tickets.py` needs zero changes to gain push wakeup. A poke is a
*hint*: the sweep re-queries the API and is idempotent, so a redundant one
costs a wasted round trip and nothing else.

**A stream outage degrades to today's behaviour, never to a stalled resolver.**
Reconnects back off to a cap and keep retrying forever, and the timer stays
installed as the safety net — for missed events, for a listener that is down,
and for tickets that become actionable with no event at all (a due date
passing). Nothing here is load-bearing for correctness.

Run it under `stingray-resolver-listen.service`, or by hand:

    ./listen.py                 # follow the stream, poke for real
    ./listen.py --dry-run       # log what it would poke, start nothing
    ./listen.py --once          # exit after the first poke (smoke test)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Any, Iterator, Optional

import requests

import audit
from config import Config

# Event types worth waking for. Everything else on the stream (status changes,
# comments, agent runs) is a ticket already moving, which is either the
# resolver's own work echoing back or a human's — neither needs a sweep.
WAKE_EVENTS = frozenset({"ticket.assigned"})

DEFAULT_UNIT = "stingray-resolver.service"

# How long to hold the door open after the first event before poking. Bulk
# reassignment fans out one event per ticket; the sweep works the whole queue in
# one pass, so waiting a beat turns a burst of N into one start.
COALESCE_SECONDS = 2.0

# Read timeout on the stream. The server heartbeats every 15s, so silence for
# appreciably longer than that means the connection is dead — usually a proxy or
# a NAT table that dropped it without sending a FIN, which is exactly the case a
# socket read would otherwise block on forever.
READ_TIMEOUT = 45.0
CONNECT_TIMEOUT = 15.0

RECONNECT_BACKOFF_START = 2.0
RECONNECT_BACKOFF_CAP = 300.0


def log(msg: str) -> None:
    audit.get_logger().info(msg)


class _Formatter(logging.Formatter):
    """Plain lines, with the API key scrubbed the same way a sweep scrubs it."""

    def format(self, record: logging.LogRecord) -> str:
        return audit.redact(super().format(record))


def setup_logging(cfg: Config) -> None:
    """Log to stdout only — systemd captures it into the journal.

    Deliberately *not* ``audit.setup_logging``: that opens a per-sweep log file
    and an audit jsonl, which suits a process that starts, does one unit of work
    and exits. This one runs for weeks and its whole output is connection
    bookkeeping, so a file per run would be a file that never rotates.
    """
    audit.register_secret(cfg.api_key)
    logger = audit.get_logger()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)


# --- SSE parsing ------------------------------------------------------------

def parse_sse(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Turn a stream of decoded lines into ``{id, event, data}`` frames.

    A blank line dispatches the frame; a line starting with ``:`` is a comment
    (the server's heartbeat) and is dropped, though merely *arriving* is the
    signal that the connection is alive. Unparseable ``data:`` is passed through
    as None rather than killing the connection — a frame this client does not
    understand is not a reason to stop following the stream.
    """
    event: Optional[str] = None
    event_id: Optional[str] = None
    data_lines: list[str] = []

    for line in lines:
        line = line.rstrip("\r")
        if line.startswith(":"):
            continue
        if line == "":
            if event is not None or data_lines:
                raw = "\n".join(data_lines)
                try:
                    data = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    data = None
                yield {"id": event_id, "event": event, "data": data}
            event, event_id, data_lines = None, None, []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)
        # Any other field (`retry:`) is not something this client acts on.


def is_wakeup(frame: dict[str, Any], bot_user_id: int) -> bool:
    """True when ``frame`` means work landed in this bot's queue.

    The server already applied its read boundary, so everything on the stream is
    something this key may see — but that boundary is "created by me OR assigned
    to me", which is wider than "mine to work". The narrowing to ``assigned_to``
    happens here so the endpoint stays a general-purpose tail.
    """
    if frame.get("event") not in WAKE_EVENTS:
        return False
    data = frame.get("data")
    if not isinstance(data, dict):
        # An assignment event we could not parse: wake anyway. The sweep filters
        # by assignee itself, so the cost of being wrong is one no-op run, while
        # the cost of ignoring it is a ticket sitting until the timer fires.
        return True
    return data.get("assigned_to") == bot_user_id


# --- the poke ---------------------------------------------------------------

class Waker:
    """Coalesces a burst of events into one `systemctl start`.

    Runs on its own thread so the coalescing window never blocks the reader —
    events that arrive mid-window have to keep being collected, or the window
    would swallow them instead of merging them.
    """

    def __init__(self, unit: str, dry_run: bool = False, once: bool = False,
                 stop: Optional[threading.Event] = None):
        self.unit = unit
        self.dry_run = dry_run
        self.once = once
        # Shared with the reader, so `--once` tears the whole daemon down rather
        # than leaving the connection held open by a thread with nothing to do.
        self._stop = stop or threading.Event()
        self._pending = threading.Event()
        self.pokes = 0
        self._thread = threading.Thread(target=self._run, name="waker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def notify(self) -> None:
        self._pending.set()

    def stop(self) -> None:
        self._stop.set()
        self._pending.set()  # unblock the wait so the thread can see the stop

    def _run(self) -> None:
        while not self._stop.is_set():
            self._pending.wait()
            if self._stop.is_set():
                return
            # Hold the window open, collecting whatever else arrives.
            if self._stop.wait(COALESCE_SECONDS):
                return
            # Clear *before* poking, not after: an event that lands while the
            # sweep is starting describes work that start may be too late to
            # see, so it has to survive as a fresh pending flag.
            self._pending.clear()
            self.poke()
            if self.once:
                self._stop.set()
                return

    def poke(self) -> None:
        self.pokes += 1
        if self.dry_run:
            log(f"listen: would start {self.unit} (dry run)")
            return
        # --no-block returns as soon as the job is queued. Without it this would
        # sit for the whole sweep, since the unit is Type=oneshot and `start`
        # waits for it; with it, systemd merges a start for an already-running
        # unit into the queued job, which is the debouncing this relies on.
        cmd = ["systemctl", "start", "--no-block", self.unit]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            log("listen: systemctl not found — is this host running systemd?")
            return
        except subprocess.TimeoutExpired:
            log(f"listen: `systemctl start {self.unit}` timed out")
            return
        if result.returncode != 0:
            log(f"listen: `systemctl start {self.unit}` failed rc={result.returncode}: "
                f"{(result.stderr or '').strip()}")
        else:
            log(f"listen: poked {self.unit}")


# --- the stream -------------------------------------------------------------

def follow(cfg: Config, waker: Waker, stop: threading.Event,
           last_event_id: Optional[str] = None) -> Optional[str]:
    """Hold one connection until it drops. Returns the last event id seen.

    Raised exceptions are the caller's signal to back off and reconnect; the
    cursor is returned so the next attempt can resume where this one stopped
    rather than skipping the gap.
    """
    url = f"{cfg.stingray_url}/api/events/stream"
    params = {"last_event_id": last_event_id} if last_event_id else {}
    headers = {"X-API-Key": cfg.api_key, "Accept": "text/event-stream"}

    with requests.get(url, headers=headers, params=params, stream=True,
                      timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as response:
        response.raise_for_status()
        log(f"listen: connected to {url}"
            + (f" (resuming after {last_event_id})" if last_event_id else ""))
        for frame in parse_sse(response.iter_lines(decode_unicode=True)):
            if stop.is_set():
                break
            if frame.get("id"):
                last_event_id = frame["id"]
            if is_wakeup(frame, cfg.bot_user_id):
                data = frame.get("data") or {}
                log(f"listen: {frame.get('event')} ticket={data.get('ticket_id')} "
                    f"-> waking the resolver")
                waker.notify()
    return last_event_id


def run(cfg: Config, waker: Waker, stop: threading.Event) -> None:
    """Follow the stream forever, reconnecting with backoff on any drop."""
    backoff = RECONNECT_BACKOFF_START
    last_event_id: Optional[str] = None

    while not stop.is_set():
        try:
            last_event_id = follow(cfg, waker, stop, last_event_id)
            # A clean return means the server closed the stream. That is not an
            # error, but reconnecting instantly would spin if it keeps happening.
            backoff = RECONNECT_BACKOFF_START
            reason = "stream closed by the server"
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            reason = f"HTTP {status}"
            if status in (401, 403):
                # A bad key will never fix itself by retrying sooner. Keep
                # retrying anyway (the key may be rotated back in) but at the cap.
                log("listen: the API key was rejected — check STINGRAY_API_KEY")
                backoff = RECONNECT_BACKOFF_CAP
        except requests.RequestException as exc:
            reason = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - the daemon must outlive surprises
            reason = f"unexpected {type(exc).__name__}: {exc}"

        if stop.is_set():
            break
        log(f"listen: disconnected ({reason}); reconnecting in {backoff:.0f}s. "
            "The resolver timer covers the gap.")
        if stop.wait(backoff):
            break
        backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--unit", default=os.environ.get("RESOLVER_UNIT", DEFAULT_UNIT),
                        help=f"systemd unit to start on an event (default {DEFAULT_UNIT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="log the pokes instead of starting anything")
    parser.add_argument("--once", action="store_true",
                        help="exit after the first poke (smoke test)")
    args = parser.parse_args(argv)

    cfg = Config.load()
    setup_logging(cfg)
    log(f"listen: following {cfg.stingray_url} as user {cfg.bot_user_id}, "
        f"unit={args.unit}{' (dry run)' if args.dry_run else ''}")

    stop = threading.Event()
    waker = Waker(args.unit, dry_run=args.dry_run, once=args.once, stop=stop)
    waker.start()

    def _shutdown(signum, _frame):
        log(f"listen: caught {signal.Signals(signum).name}, shutting down")
        stop.set()
        waker.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        run(cfg, waker, stop)
    except KeyboardInterrupt:
        pass
    finally:
        waker.stop()
    log("listen: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
