"""Event envelope shared by the WebSocket bridge and the UI (TZ §3.1).

Event types: state, transcript, answer, tool_call, error, download_progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

STATE = "state"
TRANSCRIPT = "transcript"
ANSWER = "answer"
TOOL_CALL = "tool_call"
ERROR = "error"
DOWNLOAD_PROGRESS = "download_progress"


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


def state_event(state: str) -> Event:
    return Event(STATE, {"state": state})


def transcript_event(text: str, source: str = "voice") -> Event:
    return Event(TRANSCRIPT, {"text": text, "source": source})


def answer_event(text: str, replaced_transcript: str | None = None) -> Event:
    return Event(ANSWER, {"text": text, "replaced_transcript": replaced_transcript})


def tool_call_event(name: str, args: dict[str, Any], ok: bool) -> Event:
    return Event(TOOL_CALL, {"name": name, "args": args, "ok": ok})


def error_event(message: str, kind: str = "generic") -> Event:
    return Event(ERROR, {"message": message, "kind": kind})


def download_progress_event(stage: str, percent: float, message: str = "") -> Event:
    return Event(DOWNLOAD_PROGRESS, {"stage": stage, "percent": round(percent, 1), "message": message})


EventSink = Callable[[Event], None]
