"""WebSocket fan-out for UI events (TZ §3.1).

The runtime emits events from the asyncio loop (and occasionally from worker
threads), so the hub queues them and a single background task broadcasts.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket

from .events import Event
from .logging_setup import get_logger

log = get_logger("ws")


class EventHub:
    def __init__(self, max_queue: int = 500) -> None:
        self._clients: set[WebSocket] = set()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._task is None or self._task.done():
            self._task = self._loop.create_task(self._drain(), name="vox-event-hub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._clients.clear()

    # ------------------------------------------------------------------ clients
    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ------------------------------------------------------------------ events
    def emit(self, event: Event) -> None:
        """Thread-safe, never blocks the caller."""
        payload = event.to_json()
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._put(payload)
        else:
            try:
                loop.call_soon_threadsafe(self._put, payload)
            except RuntimeError:  # pragma: no cover - loop shutting down
                pass

    def _put(self, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Drop the oldest event instead of stalling the assistant.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(payload)
            except Exception:  # pragma: no cover - defensive
                pass

    async def _drain(self) -> None:
        while True:
            payload = await self._queue.get()
            await self._broadcast(payload)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        message = json.dumps(payload, ensure_ascii=False)
        for client in list(self._clients):
            try:
                await client.send_text(message)
            except Exception:
                self.disconnect(client)
