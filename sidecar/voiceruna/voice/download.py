"""Model downloader with progress events (TZ 13, sprint 3).

Everything lands in %APPDATA%/VoiceRunaCore/models (paths.models_dir); the
installation directory is never written to.

    whisper   Systran/faster-whisper-{stt.model} from HuggingFace, the exact
              file list comes from the HF tree API
    vosk      vosk-model-small-ru-0.22.zip from alphacephei, unpacked in place
    vad       silero_vad.onnx for the VAD

Progress is reported as download_progress events with a percentage by bytes.
A file that is already present is never downloaded twice; an interrupted
transfer is resumed with a Range request from the .part file, transport errors
are retried, and a network error turns into an error event plus False - it
never raises into the caller.

httpx is imported lazily so the module (and the whole voice package) can be
imported without it; tests inject an AsyncClient built on MockTransport.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import zipfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .. import paths
from ..events import download_progress_event, error_event

HF_ENDPOINT = "https://huggingface.co"
WHISPER_REPO = "Systran/faster-whisper-{model}"
WHISPER_FILES = (
    "config.json",
    "tokenizer.json",
    "vocabulary.txt",
    "model.bin",
    "preprocessor_config.json",
)

VOSK_MODEL_DIR = "vosk-model-small-ru-0.22"
VOSK_ARCHIVE_NAME = f"{VOSK_MODEL_DIR}.zip"
#: alphacephei.com became unusable (tens of bytes per second), so the archive
#: comes from mirrors that serve exactly the same bytes. The SHA256 below is the
#: official file, so a mirror can only be trusted after it matches.
VOSK_URLS = (
    "https://huggingface.co/rhasspy/vosk-models/resolve/main/ru/vosk-model-small-ru-0.22.zip",
    "https://raw.githubusercontent.com/kercre123/vosk-models/main/vosk-model-small-ru-0.22.zip",
    "https://hf-mirror.com/rhasspy/vosk-models/resolve/main/ru/vosk-model-small-ru-0.22.zip",
)
#: Primary mirror, kept as a name for callers that only want one URL.
VOSK_URL = VOSK_URLS[0]
#: Exact size and digest of the official vosk-model-small-ru-0.22.zip.
VOSK_SIZE_BYTES = 46_236_750
VOSK_SHA256 = "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"
#: One round tries every mirror once; two rounds keep a dead network from hanging.
VOSK_DOWNLOAD_ROUNDS = 2

SILERO_DIR = "silero-vad"
SILERO_FILE = "silero_vad.onnx"
SILERO_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/"
    "src/silero_vad/data/silero_vad.onnx"
)
SILERO_EXPECTED_BYTES = 2_300_000

STAGES = ("whisper", "vosk", "vad")

#: Timeouts for model transfers: (connect, read, write, pool) in seconds.
#: alphacephei.com serves the 46 MB vosk archive with long stalls, so "read"
#: must be far larger than the 60 s used for small metadata requests.
DOWNLOAD_TIMEOUT = {"connect": 20.0, "read": 180.0, "write": 60.0, "pool": 20.0}
#: Minimum gap between two progress events (0 % and 100 % are always sent).
PROGRESS_MIN_INTERVAL_SEC = 0.1
#: A mirror that stops sending bytes entirely is dead for this run: abort the
#: attempt after this many seconds of silence and retry (resume keeps progress).
#: Without it a stalled server keeps the UI on "downloading" for the whole
#: 180-second read timeout, three times in a row.
DOWNLOAD_STALL_SEC = 30.0
#: One interrupted transfer is normal on a home connection; retry it.
DOWNLOAD_ATTEMPTS = 3
#: Pause before attempt 2 and 3; the last value is reused for any later attempt.
DOWNLOAD_RETRY_DELAYS = (1.0, 3.0)

log = logging.getLogger(__name__)


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """SHA256 of a file, read in 1 MB blocks (runs in a worker thread)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _host_of(url: str) -> str:
    """Host part of a URL, for short log and progress messages."""
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:  # pragma: no cover - malformed URL from config
        return url


class ModelDownloader:
    """Downloads STT / wake word / VAD models with progress reporting."""

    def __init__(
        self,
        config: Any,
        emit: Callable[[Any], None],
        *,
        model_dir: str | Path | None = None,
        client_factory: Callable[[], Any] | None = None,
        timeout: float = 60.0,
        attempts: int = DOWNLOAD_ATTEMPTS,
        retry_delays: tuple[float, ...] = DOWNLOAD_RETRY_DELAYS,
        stall_timeout: float = DOWNLOAD_STALL_SEC,
    ) -> None:
        self._config = config
        self._emit = emit
        self._model_dir = Path(model_dir) if model_dir else paths.models_dir()
        self._client_factory = client_factory
        self._client: Any = None
        self._download_client_obj: Any = None
        self._timeout = float(timeout)
        self._attempts = max(1, int(attempts))
        self._retry_delays = tuple(float(value) for value in retry_delays)
        self._stall_timeout = max(1.0, float(stall_timeout))
        self._last_progress_at = 0.0
        self._pending_progress: tuple[str, float, str] | None = None

    # -- infrastructure ----------------------------------------------------
    @property
    def model_dir(self) -> Path:
        return self._model_dir

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:  # pragma: no cover - real network path
                import httpx

                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout),
                    follow_redirects=True,
                )
        return self._client

    def _download_client(self) -> Any:
        """Client for the big transfers: long read timeout, same injection point."""
        if self._download_client_obj is None:
            if self._client_factory is not None:
                self._download_client_obj = self._client_factory()
            else:  # pragma: no cover - real network path
                import httpx

                self._download_client_obj = httpx.AsyncClient(
                    timeout=httpx.Timeout(**DOWNLOAD_TIMEOUT),
                    follow_redirects=True,
                )
        return self._download_client_obj

    async def aclose(self) -> None:
        clients = [self._client, self._download_client_obj]
        self._client = None
        self._download_client_obj = None
        closed: set[int] = set()
        for client in clients:
            if client is None or id(client) in closed:
                continue
            closed.add(id(client))
            close = getattr(client, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # pragma: no cover
                    pass

    def _progress(self, stage: str, percent: float, message: str = "") -> None:
        """Emit a progress event, throttled to ~10/s.

        A 500 MB model produces thousands of chunk events; flooding the
        WebSocket makes the window stutter, so intermediate updates are
        coalesced while 0 % and 100 % always go through.
        """
        value = max(0.0, min(100.0, float(percent)))
        now = time.monotonic()
        if 0.0 < value < 100.0 and now - self._last_progress_at < PROGRESS_MIN_INTERVAL_SEC:
            self._pending_progress = (stage, value, message)
            return
        self._last_progress_at = now
        self._pending_progress = None
        try:
            self._emit(download_progress_event(stage, value, message))
        except Exception:  # pragma: no cover - the sink must never break a download
            log.debug("progress sink failed", exc_info=True)

    def _fail(self, message: str, exc: BaseException | None = None) -> None:
        """Report a failure; the UI toast keeps a short reason such as (ReadTimeout)."""
        reason = f" ({type(exc).__name__})" if exc is not None else ""
        detail = f"{message}{reason}: {exc}" if exc is not None else message
        log.warning("model download failed: %s", detail)
        try:
            self._emit(error_event(message + reason, "download"))
        except Exception:  # pragma: no cover
            pass

    @staticmethod
    def _percent(done: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return 100.0 * min(1.0, done / total)

    async def _vosk_archive_valid(self, archive: Path) -> bool:
        """An existing archive is reused only when its size and SHA256 match."""
        if not archive.is_file():
            return False
        try:
            size = archive.stat().st_size
        except OSError:  # pragma: no cover - racing file deletion
            return False
        if size != VOSK_SIZE_BYTES:
            return False
        digest = await asyncio.to_thread(_sha256_file, archive)
        return digest == VOSK_SHA256

    # -- readiness ---------------------------------------------------------
    def whisper_ready(self, model: str | None = None) -> bool:
        name = self._whisper_model(model)
        directory = self._model_dir / f"whisper-{name}"
        return (directory / "model.bin").is_file() and (directory / "config.json").is_file()

    def vosk_ready(self) -> bool:
        directory = self._model_dir / VOSK_MODEL_DIR
        return (directory / "conf").is_dir() or (directory / "am").is_dir()

    def vad_ready(self) -> bool:
        model = self._model_dir / SILERO_DIR / SILERO_FILE
        return model.is_file() and model.stat().st_size > 1024

    def _whisper_model(self, model: str | None = None) -> str:
        if model:
            return str(model)
        try:
            return str(self._config.get("stt.model", "small") or "small")
        except Exception:  # pragma: no cover
            return "small"

    # -- entry point -------------------------------------------------------
    async def ensure_models(self, stage: str = "all") -> bool:
        """Download whatever the given stage needs; False when something failed."""
        name = str(stage or "all").strip().lower()
        ok = True
        if name in ("all", "whisper", "stt"):
            ok = await self.ensure_whisper() and ok
        if name in ("all", "vosk", "wake"):
            if self._wake_engine() == "vosk":
                ok = await self.ensure_vosk() and ok
        if name in ("all", "vad"):
            ok = await self.ensure_vad() and ok
        return ok

    def _wake_engine(self) -> str:
        try:
            return str(self._config.get("wakeword.engine", "vosk") or "none").strip().lower()
        except Exception:  # pragma: no cover
            return "none"

    # -- whisper -----------------------------------------------------------
    async def ensure_whisper(self, model: str | None = None) -> bool:
        name = self._whisper_model(model)
        if self.whisper_ready(name):
            self._progress("whisper", 100.0, "модель распознавания уже загружена")
            return True
        stage = "whisper"
        try:
            repo = name if "/" in name else WHISPER_REPO.format(model=name)
            files = await self._list_hf_files(repo)
            if not files:
                files = [{"path": item, "size": 0} for item in WHISPER_FILES]
            total = sum(int(item.get("size") or 0) for item in files)
            directory = self._model_dir / f"whisper-{name}"
            directory.mkdir(parents=True, exist_ok=True)
            done = 0
            self._progress(stage, 0.0, "скачиваю модель распознавания")
            for item in files:
                relative = str(item.get("path") or "").strip()
                if not relative:
                    continue
                target = directory / relative
                size = int(item.get("size") or 0)
                if target.is_file() and target.stat().st_size > 0:
                    done += size or target.stat().st_size
                    self._progress(stage, self._percent(done, total), f"{relative} уже загружен")
                    continue
                url = f"{HF_ENDPOINT}/{repo}/resolve/main/{relative}"
                transferred = await self._download_file(url, target, stage, done, total)
                done += transferred or size
                self._progress(stage, self._percent(done, total), relative)
            if self.whisper_ready(name):
                self._progress(stage, 100.0, "модель распознавания готова")
                return True
            self._fail("модель распознавания скачана не полностью")
            return False
        except Exception as exc:
            self._fail("не удалось скачать модель распознавания", exc)
            return False

    async def _list_hf_files(self, repo: str) -> list[dict[str, Any]]:
        url = f"{HF_ENDPOINT}/api/models/{repo}/tree/main"
        response = await self._get_client().get(url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        result: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in (None, "file"):
                continue
            relative = str(item.get("path") or item.get("rfilename") or "")
            if relative in WHISPER_FILES:
                result.append({"path": relative, "size": int(item.get("size") or 0)})
        return result

    # -- vosk --------------------------------------------------------------
    async def ensure_vosk(self) -> bool:
        """Download and unpack the wake word model, trying every mirror.

        The official alphacephei host became unusable, so the archive is fetched
        from mirrors of the same file. Each download is verified against
        VOSK_SIZE_BYTES and VOSK_SHA256: a truncated or replaced file is deleted
        and the next mirror is tried, so a bad mirror can never leave a
        half-working KWS model behind.

        One round tries every mirror once (Range resume and transport retries
        stay inside a single URL). After a full round the usual retry pause
        happens, and at most VOSK_DOWNLOAD_ROUNDS rounds are made.
        """
        stage = "vosk"
        if self.vosk_ready():
            self._progress(stage, 100.0, "модель wake word уже загружена")
            return True
        archive = self._model_dir / VOSK_ARCHIVE_NAME
        try:
            if await self._vosk_archive_valid(archive):
                log.info("vosk archive is already downloaded and valid")
                self._progress(stage, 100.0, "архив модели wake word уже скачан")
                return await self._install_vosk_archive(archive)

            last_error: BaseException | None = None
            for round_index in range(1, VOSK_DOWNLOAD_ROUNDS + 1):
                for url in VOSK_URLS:
                    host = _host_of(url)
                    try:
                        self._progress(stage, 0.0, f"скачиваю модель wake word: {host}")
                        started = time.perf_counter()
                        await self._download_file(url, archive, stage, 0, VOSK_SIZE_BYTES)
                        elapsed = max(1e-6, time.perf_counter() - started)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        log.warning("vosk mirror %s failed: %s: %s", host, type(exc).__name__, exc)
                        continue
                    if not await self._vosk_archive_valid(archive):
                        size = archive.stat().st_size if archive.is_file() else 0
                        log.warning(
                            "vosk mirror %s returned a bad archive (%d bytes, expected %d), dropping it",
                            host,
                            size,
                            VOSK_SIZE_BYTES,
                        )
                        self._discard(archive)
                        last_error = RuntimeError(f"{host}: размер или SHA256 не совпали")
                        continue
                    size = archive.stat().st_size
                    log.info(
                        "vosk model downloaded from %s: %.1f MB in %.1f s (%.1f MB/s)",
                        url,
                        size / 1_000_000,
                        elapsed,
                        size / 1_000_000 / elapsed,
                    )
                    return await self._install_vosk_archive(archive)
                if round_index < VOSK_DOWNLOAD_ROUNDS:
                    await self._retry_delay(round_index)
            self._fail("не удалось скачать модель wake word со всех зеркал", last_error)
            return False
        except Exception as exc:
            self._fail("не удалось скачать модель wake word", exc)
            return False

    async def _install_vosk_archive(self, archive: Path) -> bool:
        """Unpack a verified archive and report whether the model is usable."""
        try:
            await asyncio.to_thread(self._extract_zip, archive)
        except Exception as exc:
            # The bytes were verified, so this is a local unpack problem; keep
            # the archive for the next attempt instead of downloading 46 MB again.
            self._fail("не удалось распаковать модель wake word", exc)
            return False
        if self.vosk_ready():
            self._progress("vosk", 100.0, "модель wake word готова")
            return True
        self._fail("архив модели wake word распакован не полностью")
        return False

    def _extract_zip(self, archive: Path) -> None:
        """Unpack into models/ with a path-traversal guard, then drop the archive."""
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                pure = PurePosixPath(member)
                if pure.is_absolute() or ".." in pure.parts:
                    raise ValueError(f"unsafe archive member: {member}")
            bundle.extractall(self._model_dir)
        try:
            archive.unlink()
        except OSError:  # pragma: no cover
            log.debug("cannot remove %s", archive)

    # -- vad ---------------------------------------------------------------
    async def ensure_vad(self) -> bool:
        if self.vad_ready():
            self._progress("vad", 100.0, "модель VAD уже загружена")
            return True
        stage = "vad"
        target = self._model_dir / SILERO_DIR / SILERO_FILE
        try:
            self._progress(stage, 0.0, "скачиваю модель VAD")
            await self._download_file(SILERO_URL, target, stage, 0, SILERO_EXPECTED_BYTES)
            if self.vad_ready():
                self._progress(stage, 100.0, "модель VAD готова")
                return True
            self._fail("модель VAD скачана не полностью")
            return False
        except Exception as exc:
            self._fail("не удалось скачать модель VAD", exc)
            return False

    # -- transfer ----------------------------------------------------------
    async def _download_file(
        self,
        url: str,
        target: Path,
        stage: str,
        base_done: int,
        total: int,
    ) -> int:
        """Stream one file to target with resume, retries and byte progress.

        The payload goes to target.name + ".part" first and is renamed only when
        complete, so a crash never leaves a truncated model behind.

        Resume: when a .part file is already there it is continued with a
        Range request instead of being thrown away; a server that answers 200
        instead of 206 simply restarts the file. Transport errors (read
        timeouts, dropped connections) are retried up to self._attempts times
        and keep the .part between attempts; the .part is removed only when
        every attempt is exhausted or the server answers a permanent 4xx.

        Returns the size the file now has on disk (resumed part + new bytes).
        """
        import httpx

        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        client = self._download_client()
        last_error: BaseException | None = None

        for attempt in range(1, self._attempts + 1):
            existing = partial.stat().st_size if partial.is_file() else 0
            headers = {"Accept-Encoding": "identity"}  # exact bytes, no gzip wrapper
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"
            if existing > 0 and total > 0:
                # Show the resumed position at once, so the bar does not look stuck.
                self._progress(
                    stage,
                    self._percent(base_done + existing, total),
                    target.name,
                )
            transferred = 0
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    status = response.status_code
                    if existing > 0 and status == 416:
                        # The partial file cannot be continued: start it clean.
                        self._discard(partial)
                        last_error = RuntimeError("HTTP 416: partial file is not usable")
                        log.warning("%s: %s, restarting from zero", target.name, last_error)
                        await self._retry_delay(attempt)
                        continue
                    if existing > 0 and status == 200:
                        log.info("%s: server ignored the Range request, restarting", target.name)
                        existing = 0
                    if existing > 0 and status == 206:
                        content_range = str(response.headers.get("content-range", "") or "")
                        if content_range and not content_range.startswith(f"bytes {existing}-"):
                            # The file changed on the server: appending would
                            # produce a silently corrupt model.
                            self._discard(partial)
                            last_error = RuntimeError(f"unexpected Content-Range {content_range!r}")
                            log.warning("%s: %s, restarting from zero", target.name, last_error)
                            await self._retry_delay(attempt)
                            continue
                    response.raise_for_status()
                    mode = "ab" if existing > 0 else "wb"
                    # No chunk_size: with one, httpx buffers until the buffer is
                    # full and a mid-body timeout loses everything, which makes
                    # Range resume useless. Without it every network chunk is
                    # handed over immediately.
                    # Pull chunk by chunk with a stall guard: a mirror that
                    # stops sending must fail fast into the retry path instead
                    # of hanging on the long read timeout.
                    chunks = response.aiter_bytes().__aiter__()
                    with partial.open(mode) as handle:
                        while True:
                            try:
                                chunk = await asyncio.wait_for(
                                    chunks.__anext__(), timeout=self._stall_timeout
                                )
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError as exc:
                                raise httpx.ReadTimeout(
                                    f"{target.name}: no data for {self._stall_timeout:.0f} s"
                                ) from exc
                            if not chunk:
                                continue
                            handle.write(chunk)
                            transferred += len(chunk)
                            if total > 0:
                                self._progress(
                                    stage,
                                    self._percent(base_done + existing + transferred, total),
                                    target.name,
                                )
                os.replace(partial, target)
                return os.path.getsize(target)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                last_error = exc
                if 400 <= status < 500 and status != 416:
                    self._discard(partial)
                    raise
                log.warning("%s: HTTP %s on attempt %d/%d", target.name, status, attempt, self._attempts)
            except httpx.TransportError as exc:
                last_error = exc
                log.warning(
                    "%s: %s on attempt %d/%d, %d bytes already saved",
                    target.name,
                    type(exc).__name__,
                    attempt,
                    self._attempts,
                    partial.stat().st_size if partial.is_file() else 0,
                )
            except OSError as exc:
                last_error = exc
                log.warning("%s: %s on attempt %d/%d", target.name, type(exc).__name__, attempt, self._attempts)
            await self._retry_delay(attempt)

        self._discard(partial)
        raise last_error if last_error is not None else RuntimeError(f"cannot download {url}")

    async def _retry_delay(self, attempt: int) -> None:
        """Sleep between attempts; the last configured delay is reused."""
        if attempt >= self._attempts or not self._retry_delays:
            return
        delay = self._retry_delays[min(attempt - 1, len(self._retry_delays) - 1)]
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover
            log.debug("cannot remove %s", path)


__all__ = [
    "DOWNLOAD_ATTEMPTS",
    "DOWNLOAD_RETRY_DELAYS",
    "DOWNLOAD_STALL_SEC",
    "DOWNLOAD_TIMEOUT",
    "HF_ENDPOINT",
    "ModelDownloader",
    "SILERO_DIR",
    "SILERO_FILE",
    "SILERO_URL",
    "STAGES",
    "VOSK_ARCHIVE_NAME",
    "VOSK_DOWNLOAD_ROUNDS",
    "VOSK_MODEL_DIR",
    "VOSK_SHA256",
    "VOSK_SIZE_BYTES",
    "VOSK_URL",
    "VOSK_URLS",
    "WHISPER_FILES",
    "WHISPER_REPO",
]