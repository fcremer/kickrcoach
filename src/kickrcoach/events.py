"""Bounded single-consumer diagnostics. Adapter state remains authoritative."""
import asyncio
import logging
from .domain import AdapterEvent, Clock, DiagnosticEvent, ReceivedAt, SystemClock

log = logging.getLogger(__name__)


class EventSource:
    def __init__(self, clock: Clock | None = None, event_capacity: int = 256) -> None:
        if event_capacity < 1:
            raise ValueError("event_capacity must be positive")
        self.clock = clock or SystemClock()
        self.events: asyncio.Queue[AdapterEvent] = asyncio.Queue(event_capacity)
        self.dropped_events = 0

    def publish(self, event: AdapterEvent) -> None:
        if self.events.full():
            self.events.get_nowait()
            self.events.task_done()
            self.dropped_events += 1
        self.events.put_nowait(event)

    def diagnostic(self, kind: str, detail: str) -> None:
        log.info("adapter_event kind=%s detail=%s", kind, detail)
        self.publish(DiagnosticEvent(ReceivedAt.now(self.clock), kind, detail))
