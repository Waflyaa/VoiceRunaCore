"""Assistant states (TZ §3.2) and a tiny observer registry."""

from __future__ import annotations

import threading
from typing import Callable

IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
PAUSED = "paused"
DOWNLOADING = "downloading"
ERROR = "error"

ALL_STATES = (IDLE, LISTENING, THINKING, SPEAKING, PAUSED, DOWNLOADING, ERROR)


class StateManager:
    """Holds the current assistant state and notifies subscribers on change.

    Notifications are synchronous and must stay cheap: the runtime forwards them
    into the WebSocket bridge, which fans out to the UI.
    """

    def __init__(self, initial: str = IDLE) -> None:
        self._state = initial
        self._listeners: list[Callable[[str], None]] = []
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def subscribe(self, listener: Callable[[str], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def set(self, state: str) -> None:
        if state not in ALL_STATES:
            raise ValueError(f"unknown state: {state}")
        with self._lock:
            changed = state != self._state
            self._state = state
            listeners = list(self._listeners)
        if changed:
            for listener in listeners:
                try:
                    listener(state)
                except Exception:  # listeners must never break the state machine
                    pass
