"""Progress events must not flood the WebSocket during a large model download."""

from __future__ import annotations

import time
from pathlib import Path

from voiceruna.voice.download import ModelDownloader, PROGRESS_MIN_INTERVAL_SEC


class _Config:
    def get(self, key, default=None):
        return default


def _downloaders(tmp_path: Path, sink: list):
    return ModelDownloader(_Config(), sink.append, model_dir=tmp_path)


def test_rapid_progress_is_coalesced(tmp_path: Path):
    events: list = []
    downloader = _downloaders(tmp_path, events)

    downloader._progress("whisper", 0.0, "start")
    for step in range(500):
        downloader._progress("whisper", step / 10.0, "chunk")
    downloader._progress("whisper", 100.0, "done")

    assert len(events) <= 4, f"слишком много событий: {len(events)}"
    assert events[0].data["percent"] == 0.0
    assert events[-1].data["percent"] == 100.0


def test_progress_goes_through_after_the_interval(tmp_path: Path):
    events: list = []
    downloader = _downloaders(tmp_path, events)

    downloader._progress("vosk", 10.0, "a")
    time.sleep(PROGRESS_MIN_INTERVAL_SEC + 0.02)
    downloader._progress("vosk", 20.0, "b")

    percents = [event.data["percent"] for event in events]
    assert percents == [10.0, 20.0]
