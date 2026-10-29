"""FastAPI sidecar: REST + WebSocket surface for the Tauri shell (TZ §3.1).

The process binds 127.0.0.1 only and requires the X-Vox-Token header (or a
?token= query parameter for the WebSocket). /health is the only unauthenticated
route: the shell polls it while the sidecar boots.
"""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import paths
from .config import ConfigManager
from .events import state_event
from .logging_setup import get_logger, setup_logging
from .runtime import AssistantRuntime
from .state import StateManager
from .ws import EventHub

log = get_logger("api")


class CommandBody(BaseModel):
    text: str = Field(default="", max_length=1000)


class EnvBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(default="", max_length=4096)


class PreviewBody(BaseModel):
    voice: str = Field(default="", max_length=128)
    text: str = Field(default="", max_length=1000)


class RecordTestBody(BaseModel):
    seconds: float = Field(default=3.0, ge=1.0, le=15.0)


class CalibrateBody(BaseModel):
    seconds: float = Field(default=3.0, ge=1.0, le=10.0)


class AudioTestBody(BaseModel):
    kind: str = Field(default="output", pattern="^(output|input|stt)$")
    device: str | int | None = None


def create_app(token: str, config_path: Any = None) -> FastAPI:
    """Build the sidecar application with its runtime, state and event hub."""
    if not token:
        raise ValueError("sidecar token is required")

    config = ConfigManager(config_path)
    config.load()
    setup_logging(config)
    state = StateManager()
    hub = EventHub()
    # One place turns state transitions into WS events: every transition of the
    # StateManager (runtime, pipeline, downloader) reaches the window.
    state.subscribe(lambda value: hub.emit(state_event(value)))
    runtime = AssistantRuntime(config, state, hub.emit)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub.start()
        await runtime.startup()
        log.info("sidecar ready (state=%s)", state.state)
        try:
            yield
        finally:
            await runtime.shutdown()
            await hub.stop()

    app = FastAPI(title="VoiceRunaCore sidecar", version="0.1.0", lifespan=lifespan)
    app.state.token = token
    app.state.config = config
    app.state.state = state
    app.state.hub = hub
    app.state.runtime = runtime
    app.state.on_shutdown = None

    # The window in a packaged Tauri app runs on http://tauri.localhost while the
    # sidecar answers on http://127.0.0.1:<port>: real cross-origin requests with
    # the X-Vox-Token header trigger a preflight. The token stays the real
    # authority and the listener is bound to 127.0.0.1, so any origin may ask.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=600,
    )

    async def require_token(x_vox_token: str | None = Header(default=None)) -> None:
        if not hmac.compare_digest(x_vox_token or "", token):
            raise HTTPException(status_code=401, detail="invalid token")

    # ------------------------------------------------------------------ liveness
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": "0.1.0", "state": state.state}

    # ------------------------------------------------------------------ dialogue
    @app.post("/command", dependencies=[Depends(require_token)])
    async def command(body: CommandBody) -> dict[str, str]:
        answer = await runtime.handle_text(body.text, source="window")
        return {"answer": answer}

    @app.post("/listen", dependencies=[Depends(require_token)])
    async def listen() -> dict[str, bool]:
        await runtime.listen()
        return {"ok": True}

    @app.post("/repeat", dependencies=[Depends(require_token)])
    async def repeat() -> dict[str, str]:
        answer = await runtime.repeat()
        return {"answer": answer}

    @app.post("/stop_speaking", dependencies=[Depends(require_token)])
    async def stop_speaking() -> dict[str, bool]:
        await runtime.stop_speaking()
        return {"ok": True}

    @app.get("/state", dependencies=[Depends(require_token)])
    async def get_state() -> dict[str, Any]:
        return {"state": state.state, "paused": runtime.paused}

    @app.post("/pause", dependencies=[Depends(require_token)])
    async def pause() -> dict[str, bool]:
        await runtime.set_paused(True)
        return {"ok": True, "paused": True}

    @app.post("/resume", dependencies=[Depends(require_token)])
    async def resume() -> dict[str, bool]:
        await runtime.set_paused(False)
        return {"ok": True, "paused": False}

    # ------------------------------------------------------------------ config
    @app.get("/config", dependencies=[Depends(require_token)])
    async def get_config() -> dict[str, Any]:
        return config.snapshot()

    @app.put("/config", dependencies=[Depends(require_token)])
    async def put_config(patch: dict[str, Any]) -> dict[str, Any]:
        snapshot = config.update(patch)
        # A provider added or edited in the settings editor must be usable at once.
        runtime.llm.reset()
        return snapshot

    @app.put("/env", dependencies=[Depends(require_token)])
    async def put_env(body: EnvBody) -> dict[str, bool]:
        config.set_env_key(body.name, body.value)
        # A brand new key un-blocks a provider that failed with 401 (TZ §7.3).
        runtime.llm.reset()
        return {"ok": True}

    # ------------------------------------------------------------------ providers
    @app.get("/providers/{provider_id}/models", dependencies=[Depends(require_token)])
    async def provider_models(provider_id: str) -> dict[str, Any]:
        if config.provider(provider_id) is None:
            raise HTTPException(status_code=404, detail="unknown provider")
        models = await runtime.llm.list_models(provider_id)
        return {"models": models}

    @app.post("/providers/{provider_id}/test", dependencies=[Depends(require_token)])
    async def provider_test(provider_id: str) -> dict[str, Any]:
        if config.provider(provider_id) is None:
            raise HTTPException(status_code=404, detail="unknown provider")
        ok, message = await runtime.llm.test_provider(provider_id)
        return {"ok": ok, "message": message}

    # ------------------------------------------------------------------ voice
    @app.get("/voices", dependencies=[Depends(require_token)])
    async def voices() -> dict[str, Any]:
        return {"voices": await runtime.voice.list_voices()}

    @app.post("/voices/open_folder", dependencies=[Depends(require_token)])
    async def voices_open_folder() -> dict[str, Any]:
        """Open the drop-in voices folder in Explorer (TZ §11)."""
        folder = paths.voices_dir()
        try:
            os.startfile(str(folder))  # noqa: S606 - the owner asked for this button
        except (AttributeError, OSError) as exc:  # pragma: no cover - non-Windows
            return {"ok": False, "path": str(folder), "message": f"не удалось открыть папку: {exc}"}
        return {"ok": True, "path": str(folder), "message": "папка голосов открыта"}

    @app.post("/tts/preview", dependencies=[Depends(require_token)])
    async def tts_preview(body: PreviewBody) -> dict[str, Any]:
        """Speak a short sample and report the real outcome (TZ 5.4, 11)."""
        try:
            result = await runtime.voice.preview(body.voice, body.text)
        except Exception as exc:  # a missing output device must not look like success
            log.warning("tts preview failed: %s", exc)
            return {"ok": False, "message": f"не удалось воспроизвести: {exc}"}
        if isinstance(result, tuple) and len(result) == 2:
            ok, message = result
        else:  # a pipeline that does not report the outcome yet
            ok, message = True, ""
        return {"ok": bool(ok), "message": str(message or "")}

    # ------------------------------------------------------------------ audio devices
    @app.get("/audio/devices", dependencies=[Depends(require_token)])
    async def audio_devices() -> dict[str, Any]:
        """Input/output devices for the settings panel plus the playback backend."""
        try:
            devices = await runtime.voice.list_devices()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("cannot list audio devices: %s", exc)
            devices = {"input": [], "output": []}
        if not isinstance(devices, dict):
            devices = {"input": [], "output": []}
        devices.setdefault("input", [])
        devices.setdefault("output", [])
        devices["current"] = {
            "input": str(config.get("audio.input_device", "default") or "default"),
            "output": str(config.get("audio.output_device", "default") or "default"),
        }
        status = getattr(runtime.voice, "playback_status", None)
        playback: dict[str, Any] = {"ok": True, "backend": "", "message": ""}
        if callable(status):
            try:
                value = status()
                if isinstance(value, dict):
                    playback.update(value)
            except Exception:  # pragma: no cover - defensive
                pass
        devices["playback"] = playback
        return devices

    @app.post("/audio/test", dependencies=[Depends(require_token)])
    async def audio_test(body: AudioTestBody) -> dict[str, Any]:
        """Play a test phrase or record a short sample (settings editor, TZ 11)."""
        try:
            if body.kind == "input":
                ok, message, level = await runtime.voice.test_input(body.device)
                return {"ok": bool(ok), "message": str(message), "level": float(level)}
            if body.kind == "stt":
                ok, message, text = await runtime.voice.test_stt()
                return {"ok": bool(ok), "message": str(message), "level": None, "text": str(text or "")}
            ok, message = await runtime.voice.test_output(body.device)
            return {"ok": bool(ok), "message": str(message), "level": None}
        except Exception as exc:
            log.warning("audio test (%s) failed: %s", body.kind, exc)
            return {"ok": False, "message": f"проверка не удалась: {exc}", "level": None}

    # ------------------------------------------------------------------ stt record test
    @app.post("/stt/record_test", dependencies=[Depends(require_token)])
    async def stt_record_test(body: RecordTestBody | None = None) -> dict[str, Any]:
        """Record from the live microphone and run the real VAD + Whisper chain.

        This is the "why does it not hear me" button: it returns the recognised
        text, a confidence, timings, the VAD segment and the saved WAV path.
        """
        seconds = float((body or RecordTestBody()).seconds)
        try:
            result = await runtime.voice.record_and_recognize(seconds)
        except Exception as exc:
            log.warning("record test failed: %s", exc)
            return {
                "ok": False,
                "text": "",
                "confidence": None,
                "processing_s": 0.0,
                "wav_path": "",
                "message": f"тест не удался: {exc}",
                "vad": {},
                "mic": {},
            }
        if not isinstance(result, dict):
            return {
                "ok": False,
                "text": "",
                "confidence": None,
                "processing_s": 0.0,
                "wav_path": "",
                "message": "тест вернул неожиданный результат",
                "vad": {},
                "mic": {},
            }
        return result

    @app.post("/audio/calibrate", dependencies=[Depends(require_token)])
    async def audio_calibrate(body: CalibrateBody | None = None) -> dict[str, Any]:
        """Measure the room noise for the current microphone and store thresholds."""
        seconds = float((body or CalibrateBody()).seconds)
        try:
            result = await runtime.voice.calibrate(seconds)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("calibration failed: %s", exc)
            return {"ok": False, "message": f"калибровка не удалась: {exc}"}
        return result

    # ------------------------------------------------------------------ diagnostics
    @app.get("/diagnostics", dependencies=[Depends(require_token)])
    async def diagnostics() -> dict[str, Any]:
        """Everything needed to explain "nothing happens" without reading logs."""
        data: dict[str, Any] = {
            "version": "0.1.0",
            "state": state.state,
            "paused": runtime.paused,
            "paths": {
                "data_dir": str(paths.data_dir()),
                "config": str(paths.config_path()),
                "env": str(paths.env_path()),
                "apps": str(paths.apps_path()),
                "models": str(paths.models_dir()),
                "logs": str(paths.logs_dir()),
            },
            "logging": {"enabled": bool(config.get("logging.enabled", False))},
        }
        providers = []
        for provider in config.get("llm.providers", []) or []:
            if not isinstance(provider, dict):
                continue
            env_name = str(provider.get("api_key_env", "") or "")
            providers.append(
                {
                    "id": provider.get("id"),
                    "enabled": bool(provider.get("enabled", True)),
                    "model": provider.get("model"),
                    "api_key_env": env_name,
                    "key_present": bool(config.env_value(env_name)) if env_name else False,
                    "supports_tools": bool(provider.get("supports_tools", True)),
                }
            )
        data["providers"] = providers
        disabled = getattr(runtime.llm, "disabled_ids", None)
        if callable(disabled):
            try:
                data["providers_disabled"] = list(disabled())
            except Exception:  # pragma: no cover - defensive
                data["providers_disabled"] = []
        voice = getattr(runtime.voice, "diagnostics", None)
        if callable(voice):
            try:
                data["voice"] = voice()
            except Exception as exc:  # pragma: no cover - defensive
                data["voice"] = {"error": f"{type(exc).__name__}: {exc}"}
        return data

    # ------------------------------------------------------------------ apps
    @app.get("/apps", dependencies=[Depends(require_token)])
    async def get_apps() -> dict[str, Any]:
        return runtime.apps.snapshot()

    @app.put("/apps", dependencies=[Depends(require_token)])
    async def put_apps(data: dict[str, Any]) -> dict[str, Any]:
        try:
            runtime.apps.replace(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.reload_apps()

    # ------------------------------------------------------------------ lifecycle
    @app.post("/shutdown", dependencies=[Depends(require_token)])
    async def shutdown(request: Request) -> dict[str, bool]:
        callback: Callable[[], None] | None = getattr(request.app.state, "on_shutdown", None)
        if callback is not None:
            asyncio.get_running_loop().call_later(0.05, callback)
        return {"ok": True}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket, token_param: str | None = Query(default=None, alias="token")) -> None:
        header_token = websocket.headers.get("x-vox-token")
        candidate = header_token or token_param or ""
        if not hmac.compare_digest(candidate, token):
            await websocket.close(code=1008)
            return
        await hub.connect(websocket)
        try:
            while True:
                # The UI does not send commands over WS; this keeps the socket open
                # and detects disconnects promptly.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:  # pragma: no cover - defensive
            log.debug("websocket dropped")
        finally:
            hub.disconnect(websocket)

    return app
