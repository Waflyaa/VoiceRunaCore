"""ModelDownloader: caching, progress events and error handling (TZ 13).

All HTTP goes through httpx.MockTransport, so the tests never touch a network.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import httpx
import pytest

from voiceruna.voice import download as download_module
from voiceruna.voice.download import (
    SILERO_DIR,
    SILERO_FILE,
    VOSK_MODEL_DIR,
    VOSK_URLS,
    WHISPER_FILES,
    ModelDownloader,
)

PAYLOAD = b"0123456789abcdef"


def hf_handler(files=WHISPER_FILES, calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        path = request.url.path
        if path.endswith("/tree/main"):
            return httpx.Response(200, json=[{"type": "file", "path": name, "size": len(PAYLOAD)} for name in files])
        for name in files:
            if path.endswith("/resolve/main/" + name):
                return httpx.Response(200, content=PAYLOAD)
        return httpx.Response(404)
    return handler


def make_downloader(config, events, handler, **kwargs) -> ModelDownloader:
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("retry_delays", (0.0, 0.0))  # keep the suite fast
    return ModelDownloader(
        config,
        events,
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        **kwargs,
    )


def progress(events) -> list[float]:
    return [
        float(event.data["percent"])
        for event in events.collected
        if event.type == "download_progress"
    ]


async def test_whisper_downloads_missing_files(config, events, data_dir):
    downloader = make_downloader(config, events, hf_handler())
    try:
        assert await downloader.ensure_whisper() is True
    finally:
        await downloader.aclose()
    directory = downloader.model_dir / "whisper-small"
    for name in WHISPER_FILES:
        assert (directory / name).read_bytes() == PAYLOAD
    assert downloader.whisper_ready() is True
    percents = progress(events)
    assert percents
    assert percents[-1] == pytest.approx(100.0)
    assert percents == sorted(percents)


async def test_whisper_is_not_downloaded_twice(config, events, data_dir):
    calls: list[str] = []
    downloader = make_downloader(config, events, hf_handler(calls=calls))
    try:
        assert await downloader.ensure_whisper() is True
        first = len(calls)
        assert await downloader.ensure_whisper() is True
        assert len(calls) == first  # cached, no new HTTP request
    finally:
        await downloader.aclose()


async def test_network_failure_reports_an_error_instead_of_raising(config, events):
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    downloader = make_downloader(config, events, broken)
    try:
        assert await downloader.ensure_whisper() is False
    finally:
        await downloader.aclose()
    errors = [event for event in events.collected if event.type == "error"]
    assert errors and errors[0].data["kind"] == "download"
    # TZ: the toast carries a short reason, not just a generic phrase
    assert "HTTPStatusError" in errors[0].data["message"]
    assert downloader.whisper_ready() is False


async def test_wake_stage_is_skipped_without_vosk_engine(config, events):
    config.set("wakeword.engine", "none")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected")

    downloader = make_downloader(config, events, handler)
    try:
        assert await downloader.ensure_models("wake") is True
    finally:
        await downloader.aclose()


def vosk_archive(payload_note: str = "x") -> bytes:
    """A tiny but structurally correct vosk archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(f"{VOSK_MODEL_DIR}/conf/model.conf", payload_note)
        bundle.writestr(f"{VOSK_MODEL_DIR}/am/final.mdl", "y")
        bundle.writestr(f"{VOSK_MODEL_DIR}/graph/HCLr.fst", "z")
    return buffer.getvalue()


def expect_vosk_archive(monkeypatch, archive: bytes) -> bytes:
    """Make the real size/SHA256 constants match the test archive."""
    monkeypatch.setattr(download_module, "VOSK_SIZE_BYTES", len(archive))
    monkeypatch.setattr(download_module, "VOSK_SHA256", hashlib.sha256(archive).hexdigest())
    return archive


async def test_vosk_archive_is_downloaded_and_extracted(config, events, monkeypatch):
    archive = expect_vosk_archive(monkeypatch, vosk_archive())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=archive)

    config.set("wakeword.engine", "vosk")
    downloader = make_downloader(config, events, handler)
    try:
        assert await downloader.ensure_models("wake") is True
    finally:
        await downloader.aclose()
    assert (downloader.model_dir / VOSK_MODEL_DIR / "conf" / "model.conf").is_file()
    assert not (downloader.model_dir / f"{VOSK_MODEL_DIR}.zip").exists()


async def test_vosk_uses_the_second_mirror_when_the_first_fails(config, events, monkeypatch):
    archive = expect_vosk_archive(monkeypatch, vosk_archive())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "huggingface.co" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, content=archive)

    downloader = make_downloader(config, events, handler)
    try:
        assert await downloader.ensure_vosk() is True
    finally:
        await downloader.aclose()

    assert any("huggingface.co" in url for url in calls)
    assert any("raw.githubusercontent.com" in url for url in calls)
    assert (downloader.model_dir / VOSK_MODEL_DIR / "conf" / "model.conf").is_file()


async def test_vosk_sha_mismatch_drops_the_file_and_tries_the_next_mirror(config, events, monkeypatch):
    archive = expect_vosk_archive(monkeypatch, vosk_archive())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "huggingface.co" in url:
            return httpx.Response(200, content=b"not the official archive")
        if "raw.githubusercontent.com" in url:
            return httpx.Response(200, content=b"still not it")
        return httpx.Response(200, content=archive)

    downloader = make_downloader(config, events, handler)
    try:
        assert await downloader.ensure_vosk() is True
    finally:
        await downloader.aclose()

    mirror = downloader.model_dir / f"{VOSK_MODEL_DIR}.zip"
    assert not mirror.exists()  # the archive is unpacked and removed
    assert (downloader.model_dir / VOSK_MODEL_DIR / "am" / "final.mdl").is_file()
    assert any("hf-mirror.com" in url for url in calls), "третий мирор должен быть испробован"
    errors = [event for event in events.collected if event.type == "error"]
    assert errors == []  # the fallback worked, no error toast


async def test_vosk_all_mirrors_fail_reports_an_error(config, events, monkeypatch):
    expect_vosk_archive(monkeypatch, vosk_archive())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500)

    downloader = make_downloader(config, events, handler)
    try:
        assert await downloader.ensure_vosk() is False
    finally:
        await downloader.aclose()

    tried = {url.split("//", 1)[1].split("/", 1)[0] for url in calls}
    assert tried == {url.split("//", 1)[1].split("/", 1)[0] for url in VOSK_URLS}
    errors = [event for event in events.collected if event.type == "error"]
    assert errors and errors[-1].data["kind"] == "download"
    assert "зеркал" in errors[-1].data["message"]
    assert not (downloader.model_dir / f"{VOSK_MODEL_DIR}.zip").exists()


async def test_vosk_valid_archive_is_not_downloaded_again(config, events, monkeypatch):
    archive = expect_vosk_archive(monkeypatch, vosk_archive())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(500)

    downloader = make_downloader(config, events, handler)
    mirror = downloader.model_dir / f"{VOSK_MODEL_DIR}.zip"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_bytes(archive)
    try:
        assert await downloader.ensure_vosk() is True
    finally:
        await downloader.aclose()

    assert calls == []  # already on disk and verified: no HTTP at all
    assert (downloader.model_dir / VOSK_MODEL_DIR / "conf" / "model.conf").is_file()


async def test_vosk_resume_continues_on_the_same_mirror(config, events, monkeypatch):
    archive = expect_vosk_archive(monkeypatch, vosk_archive())
    break_at = 40
    calls: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("Range")))
        if len(calls) == 1:
            return httpx.Response(200, stream=ChunkStream([archive[:break_at]], httpx.ReadTimeout("stall")))
        offset = 0
        if request.headers.get("Range"):
            offset = int(str(request.headers["Range"]).split("=", 1)[1].split("-", 1)[0])
        return httpx.Response(206, stream=ChunkStream([archive[offset:]]))

    downloader = make_downloader(config, events, handler)
    try:
        assert await downloader.ensure_vosk() is True
    finally:
        await downloader.aclose()

    assert calls[0][0] == calls[1][0], "докачка должна идти на том же зеркале"
    assert calls[1][1] == f"bytes={break_at}-"


async def test_vad_model_is_downloaded(config, events):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(SILERO_FILE)
        return httpx.Response(200, content=b"z" * 4096)

    downloader = make_downloader(config, events, handler)
    try:
        assert await downloader.ensure_vad() is True
        assert await downloader.ensure_vad() is True
    finally:
        await downloader.aclose()
    assert (downloader.model_dir / SILERO_DIR / SILERO_FILE).is_file()


async def test_downloader_never_writes_outside_models_dir(config, events, data_dir):
    downloader = make_downloader(config, events, hf_handler())
    try:
        await downloader.ensure_whisper()
    finally:
        await downloader.aclose()
    models = data_dir / "models"
    written = {path.relative_to(models).parts[0] for path in models.rglob("*") if path.is_file()}
    assert written == {"whisper-small"}

# --------------------------------------------------------------------------- resume
FULL = bytes(range(256)) * 16  # 4096 bytes, big enough for vad_ready()
BREAK_AT = 1500


class ChunkStream(httpx.AsyncByteStream):
    """Yields the given chunks and then fails, like a dropped connection."""

    def __init__(self, chunks, error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


def interrupting_handler(state: dict, *, honour_range: bool = True, full: bytes = FULL):
    """First request breaks mid-body; later requests serve the rest."""

    def handler(request: httpx.Request) -> httpx.Response:
        ranges = state.setdefault("ranges", [])
        ranges.append(request.headers.get("Range"))
        if len(ranges) == 1:
            return httpx.Response(
                200,
                stream=ChunkStream([full[:BREAK_AT]], httpx.ReadTimeout("simulated stall")),
            )
        start = 0
        if honour_range and ranges[-1]:
            start = int(str(ranges[-1]).split("=", 1)[1].split("-", 1)[0])
        if start:
            return httpx.Response(
                206,
                stream=ChunkStream([full[start:]]),
                headers={"Content-Range": f"bytes {start}-{len(full) - 1}/{len(full)}"},
            )
        return httpx.Response(200, stream=ChunkStream([full]))

    return handler


async def test_interrupted_transfer_resumes_with_range(config, events):
    """A read timeout keeps the .part file and the next attempt continues it."""
    state: dict = {}
    downloader = make_downloader(config, events, interrupting_handler(state))
    try:
        assert await downloader.ensure_vad() is True
    finally:
        await downloader.aclose()

    target = downloader.model_dir / SILERO_DIR / SILERO_FILE
    assert target.read_bytes() == FULL
    assert not target.with_name(target.name + ".part").exists()
    # first attempt from zero, second one asks only for the missing tail
    assert state["ranges"] == [None, f"bytes={BREAK_AT}-"]
    percents = progress(events)
    assert percents == sorted(percents)  # progress never rolls back
    assert percents[-1] == pytest.approx(100.0)


async def test_server_ignoring_range_restarts_from_zero(config, events):
    """A 200 answer to a Range request must overwrite the .part, not append."""
    state: dict = {}
    downloader = make_downloader(config, events, interrupting_handler(state, honour_range=False))
    try:
        assert await downloader.ensure_vad() is True
    finally:
        await downloader.aclose()

    target = downloader.model_dir / SILERO_DIR / SILERO_FILE
    assert target.read_bytes() == FULL
    assert state["ranges"] == [None, f"bytes={BREAK_AT}-"]


async def test_resume_accounts_for_bytes_already_on_disk(config, events):
    """The resumed attempt reports the position it starts from, not zero."""
    state: dict = {}
    downloader = make_downloader(config, events, interrupting_handler(state))
    target = downloader.model_dir / SILERO_DIR / SILERO_FILE
    try:
        written = await downloader._download_file("https://example.test/blob", target, "probe", 0, len(FULL))
    finally:
        await downloader.aclose()

    assert written == len(FULL)
    assert target.read_bytes() == FULL
    percents = progress(events)
    assert percents == sorted(percents)
    assert percents[0] == pytest.approx(100.0 * BREAK_AT / len(FULL), abs=0.1)
    assert percents[-1] == pytest.approx(100.0)


async def test_all_attempts_failing_removes_partial_and_reports_reason(config, events):
    attempts: list[str | None] = []

    def always_breaks(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("Range"))
        return httpx.Response(200, stream=ChunkStream([b"x" * 900], httpx.ReadTimeout("simulated stall")))

    downloader = make_downloader(config, events, always_breaks, attempts=3)
    try:
        assert await downloader.ensure_vad() is False
    finally:
        await downloader.aclose()

    target = downloader.model_dir / SILERO_DIR / SILERO_FILE
    assert not target.exists()
    assert not target.with_name(target.name + ".part").exists()
    assert len(attempts) == 3
    errors = [event for event in events.collected if event.type == "error"]
    assert errors and "ReadTimeout" in errors[0].data["message"]


async def test_permanent_client_error_is_not_retried(config, events):
    calls: list[str | None] = []

    def not_found(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("Range"))
        return httpx.Response(404)

    downloader = make_downloader(config, events, not_found)
    try:
        assert await downloader.ensure_vad() is False
    finally:
        await downloader.aclose()

    assert len(calls) == 1  # 4xx is permanent, no second attempt
    target = downloader.model_dir / SILERO_DIR / SILERO_FILE
    assert not target.with_name(target.name + ".part").exists()

async def test_mismatched_content_range_discards_the_partial_file(config, events):
    """If the server's range answer does not match our offset, start over."""
    state = {"calls": 0, "ranges": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        state["ranges"].append(request.headers.get("Range"))
        if state["calls"] == 1:
            return httpx.Response(200, stream=ChunkStream([FULL[:BREAK_AT]], httpx.ReadTimeout("stall")))
        if state["calls"] == 2:
            return httpx.Response(
                206,
                stream=ChunkStream([FULL[1000:]]),
                headers={"Content-Range": f"bytes 1000-{len(FULL) - 1}/{len(FULL)}"},
            )
        return httpx.Response(200, stream=ChunkStream([FULL]))

    downloader = make_downloader(config, events, handler)
    try:
        assert await downloader.ensure_vad() is True
    finally:
        await downloader.aclose()

    target = downloader.model_dir / SILERO_DIR / SILERO_FILE
    assert target.read_bytes() == FULL
    assert state["ranges"] == [None, f"bytes={BREAK_AT}-", None]