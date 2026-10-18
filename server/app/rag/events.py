"""Push notifications for document status changes.

Indexing runs on a worker thread, while event streams are consumed on the
asyncio loop. publish() may be called from any thread; each subscriber gets the
event on its own loop via call_soon_threadsafe, so no subscriber ever touches
an asyncio queue from the wrong thread.

This replaces the client polling GET /api/documents every 1.5 s while anything
was indexing: the browser now hears about a change only when one happens, and
receives just the document that changed rather than the whole list.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Bounded, so a stalled client cannot grow memory without limit. A subscriber
# that falls this far behind just misses events; the snapshot it gets on
# reconnect brings it back in sync.
_QUEUE_SIZE = 256

# Sent to every subscriber on shutdown so their streams end promptly.
SHUTDOWN = object()

_lock = threading.Lock()
_subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()


def subscribe() -> asyncio.Queue:
    """Register the calling coroutine's loop for events. Call unsubscribe() after."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
    with _lock:
        _subscribers.add((asyncio.get_running_loop(), queue))
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    with _lock:
        for entry in [s for s in _subscribers if s[1] is queue]:
            _subscribers.discard(entry)


def _deliver(queue: asyncio.Queue, event: Any) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning("Document event dropped for a slow subscriber.")


def publish(event: Any) -> None:
    """Send an event to every subscriber. Safe to call from any thread."""
    with _lock:
        targets = list(_subscribers)
    for loop, queue in targets:
        try:
            loop.call_soon_threadsafe(_deliver, queue, event)
        except RuntimeError:
            # The subscriber's loop has already closed.
            unsubscribe(queue)


def shutdown() -> None:
    """End every open stream, so the server is not held open by them."""
    publish(SHUTDOWN)


def install_signal_hook() -> None:
    """End streams as soon as the server is asked to stop.

    Uvicorn waits for in-flight responses to finish before exiting, and an
    event stream never finishes on its own -- so without this, Ctrl+C would
    hang until pressed a second time.

    Uvicorn installs its own handlers before it imports the app, so this wraps
    whatever handler is current rather than replacing it. Call it from the
    app's startup, which runs on the main thread. Uvicorn restores its saved
    originals on exit, so nothing here needs undoing.
    """
    import signal

    names = ("SIGINT", "SIGTERM", "SIGBREAK")  # SIGBREAK exists on Windows only
    for sig in (getattr(signal, n) for n in names if hasattr(signal, n)):
        previous = signal.getsignal(sig)
        if not callable(previous) or getattr(previous, "_ends_document_streams", False):
            continue

        def handler(signum, frame, _previous=previous):  # type: ignore[no-untyped-def]
            shutdown()
            return _previous(signum, frame)

        handler._ends_document_streams = True  # type: ignore[attr-defined]
        try:
            signal.signal(sig, handler)
        except ValueError:  # Not on the main thread; streams then end on disconnect.
            return
