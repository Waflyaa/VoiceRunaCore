"""Stall detection and resume behaviour of the model downloader.

A mirror that stops sending bytes must fail fast into the retry path instead of
hanging on the long read timeout, and the next attempt must resume from the
bytes already on disk.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from voiceruna.voice.download import ModelDownloader


class FakeResponse:
    """Streams the given chunks; with stall=True it then goes silent forever."""

    def __init__(self, chunks: list[bytes], *, status_code: int = 200, headers: dict | None = None, stall: bool = False) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks
        self._stall = stall

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)  # type: ignore[arg-type]

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk
        if self._stall:
            await asyncio.sleep(3600)  # a dead mirror: the body simply stops


class FakeStream:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> FakeResponse:
        return self._response

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeClient:
    """Returns a queued response per request and records the Range headers."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []

    def stream(self, method: str, url: str, headers: dict | None = None) -> FakeStream:
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {})})
        return FakeStream(self._responses.pop(0))


class _Config:
    def get(self, key, default=None):
        return default


@pytest.mark.asyncio
async def test_stalled_stream_aborts_and_reports_timeout(tmp_path: Path):
    client = FakeClient([FakeResponse([b"abcd"], stall=True) for _ in range(3)])
    downloader = ModelDownloader(
        _Config(), lambda event: None, model_dir=tmp_path, client_factory=lambda: client,
        attempts=2, retry_delays=(0,), stall_timeout=1.0,
    )
    target = tmp_path / "model.bin"

    with pytest.raises(httpx.ReadTimeout):
        await downloader._download_file("https://mirror/model.bin", target, "vosk", 0, 1000)

    assert not target.exists()
    assert not (tmp_path / "model.bin.part").exists()  # exhausted attempts clean up
    assert len(client.requests) == 2  # one attempt per configured retry


@pytest.mark.asyncio
async def test_resume_after_stall_continues_from_saved_bytes(tmp_path: Path):
    first = FakeResponse([b"abcd"], stall=True)
    second = FakeResponse(
        [b"efgh"],
        status_code=206,
        headers={"content-range": "bytes 4-7/8"},
    )
    client = FakeClient([first, second])
    downloader = ModelDownloader(
        _Config(), lambda event: None, model_dir=tmp_path, client_factory=lambda: client,
        attempts=2, retry_delays=(0,), stall_timeout=1.0,
    )
    target = tmp_path / "model.bin"

    size = await downloader._download_file("https://mirror/model.bin", target, "vosk", 0, 8)

    assert size == 8
    assert target.read_bytes() == b"abcdefgh"
    assert client.requests[1]["headers"].get("Range") == "bytes=4-"


@pytest.mark.asyncio
async def test_progress_keeps_monotonic_after_resume(tmp_path: Path):
    events: list[float] = []
    client = FakeClient([
        FakeResponse([b"ab"], stall=True),
        FakeResponse([b"cd"], status_code=206, headers={"content-range": "bytes 2-3/4"}),
    ])
    downloader = ModelDownloader(
        _Config(), lambda event: events.append(event.data.get("percent", 0.0)),
        model_dir=tmp_path, client_factory=lambda: client, attempts=2, retry_delays=(0,), stall_timeout=1.0,
    )

    await downloader._download_file("https://mirror/model.bin", tmp_path / "m.bin", "vad", 0, 4)

    assert events, "progress must be reported"
    assert events[-1] == pytest.approx(100.0, abs=0.1)
    assert all(b >= a - 0.001 for a, b in zip(events, events[1:])), "progress must not jump backwards"
