"""EventBus: loop/thread publishing, replay, slow-consumer drop, throttling.

Every test drives the bus on a real asyncio loop via asyncio.run; worker
threads are plain threading.Thread / asyncio.to_thread, never mocks.
"""

import asyncio
import threading

from pixieveil import events as ev_mod
from pixieveil.events import EventBus


def _drain(sub) -> list:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


class TestPublish:
    def test_unbound_bus_is_a_noop(self):
        bus = EventBus()
        bus.publish("study", n=1, status="ready")  # must not raise
        assert bus.last_id == 0

    def test_publish_on_loop_reaches_every_subscriber(self):
        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            a, _ = bus.subscribe()
            b, _ = bus.subscribe()
            bus.publish("study", n=7, status="ready")
            return _drain(a), _drain(b)
        a, b = asyncio.run(go())
        assert [(e.type, e.data) for e in a] == [("study", {"n": 7, "status": "ready"})]
        assert [e.id for e in a] == [e.id for e in b] == [1]

    def test_publish_from_worker_thread_arrives(self):
        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            sub, _ = bus.subscribe()
            t = threading.Thread(target=bus.publish, args=("exam",), kwargs={"n": 3})
            t.start()
            t.join()
            return await asyncio.wait_for(sub.queue.get(), timeout=2)
        e = asyncio.run(go())
        assert (e.type, e.data) == ("exam", {"n": 3})

    def test_unsubscribed_gets_nothing(self):
        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            sub, _ = bus.subscribe()
            bus.unsubscribe(sub)
            bus.publish("study", n=1, status="x")
            return _drain(sub)
        assert asyncio.run(go()) == []


class TestReplay:
    def test_replays_events_after_last_event_id(self):
        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            for i in range(5):
                bus.publish("study", n=i, status="ready")
            sub, resync = bus.subscribe(last_event_id=3)
            return [e.id for e in _drain(sub)], resync
        assert asyncio.run(go()) == ([4, 5], False)

    def test_id_older_than_ring_requests_resync(self, monkeypatch):
        monkeypatch.setattr(ev_mod, "RING_SIZE", 3)

        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            for i in range(10):
                bus.publish("study", n=i, status="ready")
            sub, resync = bus.subscribe(last_event_id=2)
            return _drain(sub), resync
        events, resync = asyncio.run(go())
        assert resync is True and events == []

    def test_id_from_a_previous_server_run_requests_resync(self):
        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            bus.publish("study", n=1, status="ready")
            return bus.subscribe(last_event_id=500)[1]
        assert asyncio.run(go()) is True


class TestSlowConsumer:
    def test_full_queue_drops_only_that_subscriber(self, monkeypatch):
        monkeypatch.setattr(ev_mod, "QUEUE_SIZE", 2)

        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            slow, _ = bus.subscribe()
            fast, _ = bus.subscribe()
            for i in range(3):
                bus.publish("study", n=i, status="ready")
                _drain(fast)
            return slow.dropped, fast.dropped, bus.subscriber_count
        assert asyncio.run(go()) == (True, False, 1)

    def test_close_all_wakes_and_drops_everyone(self):
        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            sub, _ = bus.subscribe()
            bus.close_all()
            e = sub.queue.get_nowait()
            return e.type, sub.dropped, bus.subscriber_count
        assert asyncio.run(go()) == ("_close", True, 0)


class TestThrottling:
    def test_progress_is_coalesced_but_final_and_forced_pass(self):
        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            sub, _ = bus.subscribe()
            for i in range(1, 50):
                bus.progress(1, "rhythm", i, 100)          # only the first passes
            bus.progress(1, "deface", 0, 4, label="1", force=True)
            bus.progress(1, "rhythm", 100, 100)            # final always passes
            bus.progress(2, "rhythm", 1, 100)              # other study: own window
            return [(e.data["n"], e.data["kind"], e.data["done"]) for e in _drain(sub)]
        assert asyncio.run(go()) == [
            (1, "rhythm", 1), (1, "deface", 0), (1, "rhythm", 100), (2, "rhythm", 1),
        ]

    def test_counters_throttled(self):
        async def go():
            bus = EventBus()
            bus.bind(asyncio.get_running_loop())
            sub, _ = bus.subscribe()
            for _ in range(20):
                bus.counters()
            return [e.type for e in _drain(sub)]
        assert asyncio.run(go()) == ["counters"]
