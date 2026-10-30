"""System output volume via pycaw (no UI automation, no simulated key presses).

pycaw talks to the Windows audio endpoint directly. It is imported lazily so a
missing dependency only disables these two tools instead of the whole sidecar.
"""

from __future__ import annotations

import logging

__all__ = ["apply", "change_percent", "current_percent", "is_muted", "set_muted", "set_percent"]

log = logging.getLogger(__name__)

#: One "louder"/"quieter" command changes the level by this many percent.
STEP_PERCENT = 10


def _endpoint():
    """Return the IAudioEndpointVolume interface of the default speakers."""
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioEndpointVolume)


def current_percent() -> int:
    return int(round(float(_endpoint().GetMasterVolumeLevelScalar()) * 100))


def is_muted() -> bool:
    return bool(_endpoint().GetMute())


def set_percent(percent: int) -> int:
    percent = max(0, min(100, int(percent)))
    _endpoint().SetMasterVolumeLevelScalar(percent / 100.0, None)
    return percent


def set_muted(muted: bool) -> None:
    _endpoint().SetMute(1 if muted else 0, None)


def change_percent(delta: int) -> int:
    return set_percent(current_percent() + int(delta))


def apply(action: str, level: int | None = None) -> tuple[int, bool]:
    """Run one volume action; returns (percent, muted)."""
    action = (action or "").strip().lower()
    if action == "up":
        return change_percent(STEP_PERCENT), False
    if action == "down":
        # Raising the level while muted is pointless: unmute as well.
        return change_percent(-STEP_PERCENT), False
    if action == "mute":
        set_muted(True)
        return current_percent(), True
    if action == "unmute":
        set_muted(False)
        return current_percent(), False
    if action == "set":
        if level is None:
            raise ValueError("не задан уровень громкости")
        return set_percent(int(level)), False
    raise ValueError(f"неизвестное действие: {action!r}")
