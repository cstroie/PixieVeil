"""
In-process event bus feeding the dashboard's Server-Sent Events stream.

Events are thin notifications ("study 55 changed", "study 55 deface 2/4"),
never data: the browser re-fetches through the regular, whitelisted REST
endpoints. ``publish()`` is safe to call from worker threads (defacing,
ZIP, DICOM send all run under ``asyncio.to_thread``) and is a no-op until
``bind()`` attaches the bus to the running event loop, so code paths and
tests without a dashboard behave exactly as before.
"""

import asyncio
import itertools
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

QUEUE_SIZE = 256          # per subscriber; a full queue drops that subscriber
RING_SIZE = 200           # events kept for Last-Event-ID replay
PROGRESS_INTERVAL_S = 0.5  # min spacing of progress events per (study, kind)
COUNTERS_INTERVAL_S = 2.0  # min spacing of "counters" events


@dataclass
class Event:
    id: int
    type: str
    data: dict[str, Any] = field(default_factory=dict)


class Subscriber:
    """One connected browser: a bounded queue plus a 'dropped' flag."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.dropped = False


class EventBus:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[Subscriber] = set()
        self._ring: deque[Event] = deque(maxlen=RING_SIZE)
        self._ids = itertools.count(1)
        self._last_id = 0
        self._throttle_lock = threading.Lock()
        self._last_sent: dict[tuple, float] = {}
        # Latest progress per (study, kind), kept even while unbound or
        # throttled, so a page opened mid-step can show where things stand
        # (see progress_snapshot). Dropped on the final step / clear_progress.
        self._progress_lock = threading.Lock()
        self._progress_now: dict[tuple[int, str], dict[str, Any]] = {}

    # -- lifecycle -------------------------------------------------------
    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def bound(self) -> bool:
        return self._loop is not None

    @property
    def last_id(self) -> int:
        return self._last_id

    # -- publishing ------------------------------------------------------
    def publish(self, type_: str, **data: Any) -> None:
        """Queue an event for every subscriber. Thread-safe; no-op if unbound."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._dispatch(type_, data)
        else:
            try:
                loop.call_soon_threadsafe(self._dispatch, type_, data)
            except RuntimeError:
                pass  # loop shutting down

    def progress(self, n: int, kind: str, done: int, total: int | None = None,
                 label: str | None = None, *, force: bool = False) -> None:
        """Coalesced progress event: at most one per (study, kind) every
        PROGRESS_INTERVAL_S, except the final one (done == total) or *force*.

        Every event carries ``at`` (server time) and ``since`` — when the
        current step (``done`` + ``label``) began — so a page can show how
        long it has been running."""
        now = time.time()
        final = total is not None and done >= total
        key = (n, kind)
        with self._progress_lock:
            prev = self._progress_now.get(key)
            same_step = prev is not None and prev["done"] == done and prev.get("label") == label
            data: dict[str, Any] = {"n": n, "kind": kind, "done": done, "total": total,
                                    "at": now, "since": prev["since"] if same_step else now}
            if label is not None:
                data["label"] = label
            if final:
                self._progress_now.pop(key, None)
            else:
                self._progress_now[key] = data
        if not (final or force) and not self._allow(("progress", n, kind), PROGRESS_INTERVAL_S):
            return
        self.publish("progress", **data)

    def clear_progress(self, n: int, *kinds: str) -> None:
        """Forget in-flight progress for study *n* (all kinds if none given)
        — for a step that ended without its final event (error, cancel)."""
        with self._progress_lock:
            for key in [k for k in self._progress_now
                        if k[0] == n and (not kinds or k[1] in kinds)]:
                del self._progress_now[key]

    def progress_snapshot(self) -> dict[int, dict[str, Any]]:
        """Latest in-flight progress per study (the most recently updated
        kind wins), each stamped with the current server time as ``at``."""
        now = time.time()
        out: dict[int, dict[str, Any]] = {}
        with self._progress_lock:
            for (n, _kind), data in self._progress_now.items():
                if n not in out or data["at"] > out[n]["at"]:
                    out[n] = data
        return {n: {**d, "at": now} for n, d in out.items()}

    def counters(self) -> None:
        """Throttled 'counters changed' hint for the status page."""
        if self._allow(("counters",), COUNTERS_INTERVAL_S):
            self.publish("counters")

    def _allow(self, key: tuple, interval: float) -> bool:
        now = time.monotonic()
        with self._throttle_lock:
            if now - self._last_sent.get(key, 0.0) < interval:
                return False
            self._last_sent[key] = now
            return True

    def _dispatch(self, type_: str, data: dict[str, Any]) -> None:
        """Runs on the loop thread only."""
        ev = Event(next(self._ids), type_, data)
        self._last_id = ev.id
        self._ring.append(ev)
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(ev)
            except asyncio.QueueFull:
                # A stuck tab must never back up the pipeline: drop it; the
                # browser reconnects and resyncs with a full fetch.
                logger.info("Dropping slow SSE subscriber")
                sub.dropped = True
                self._subs.discard(sub)

    # -- subscribing (loop thread) --------------------------------------
    def subscribe(self, last_event_id: int | None = None) -> tuple[Subscriber, bool]:
        """Register a subscriber. Returns (subscriber, resync): resync is True
        when *last_event_id* is given but no longer replayable (or from a
        previous server run), so the client must refetch everything."""
        sub = Subscriber()
        resync = False
        if last_event_id is not None:
            replayable = bool(self._ring) and self._ring[0].id - 1 <= last_event_id <= self._last_id
            if replayable:
                for ev in self._ring:
                    if ev.id > last_event_id:
                        sub.queue.put_nowait(ev)
            elif last_event_id != self._last_id:
                resync = True
        self._subs.add(sub)
        return sub, resync

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    def close_all(self) -> None:
        """Wake and drop every subscriber (server shutdown)."""
        for sub in list(self._subs):
            sub.dropped = True
            try:
                sub.queue.put_nowait(Event(0, "_close"))
            except asyncio.QueueFull:
                pass
        self._subs.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
