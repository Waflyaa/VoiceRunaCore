"""Assistant runtime: the orchestrator that turns text into an action and a reply.

Flow (TZ §2, §3.2): text -> Router (fast path) -> ToolExecutor | LLM cascade ->
answer -> TTS. The runtime owns the dialogue window (TZ §2.3), the 120 s reset
timer, retry semantics (TZ §2.4) and the error phrases (TZ §12).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Any

from . import paths
from .apps import AppsRegistry
from .config import ConfigManager
from .events import (
    EventSink,
    answer_event,
    error_event,
    tool_call_event,
    transcript_event,
)
from .llm.cascade import CascadeClient
from .llm.prompt import build_system_prompt
from .logging_setup import get_logger
from .router.engine import Router
from .state import ERROR, IDLE, PAUSED, THINKING, StateManager
from .tools.executor import ToolExecutor
from .voice.pipeline import VoicePipeline

log = get_logger("runtime")


class AssistantRuntime:
    """Wires config, registry, fast path, LLM cascade, tools and voice together."""

    def __init__(self, config: ConfigManager, state: StateManager, emit: EventSink) -> None:
        self._config = config
        self._state = state
        self._emit = emit

        self._apps = AppsRegistry(paths.apps_path())
        self._apps.load()

        self._router = Router(config, self._apps)
        self._tools = ToolExecutor(config, self._apps)
        self._llm = CascadeClient(config)

        max_messages = int(config.get("llm.max_messages", 8) or 8)
        self._history: deque[dict[str, Any]] = deque(maxlen=max_messages)
        self._last_user_text: str | None = None
        self._paused = False
        self._started = False
        self._reset_task: asyncio.Task[None] | None = None
        self._models_task: asyncio.Task[None] | None = None
        self._command_lock = asyncio.Lock()

        self._voice = VoicePipeline(
            config=config,
            state=state,
            emit=emit,
            on_utterance=self._on_utterance,
        )

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        self._config.subscribe(self._on_config_change)
        self._state.set(IDLE)
        try:
            await self._voice.start()
        except Exception as exc:  # a broken microphone must not kill the app
            log.error("voice pipeline failed to start: %s", exc)
            self._emit(error_event(str(exc), "voice_start"))
        self._start_model_prefetch()

    def _start_model_prefetch(self) -> None:
        """Download whisper/wake models in the background on first run (TZ §13).

        The app stays usable while the models arrive: progress is reported through
        download_progress events and the state machine shows 'downloading'.
        Set VOX_AUTODOWNLOAD=0 to skip this (CI, smoke tests, offline demos).
        """
        if os.environ.get("VOX_AUTODOWNLOAD", "1") == "0":
            return
        ensure = getattr(self._voice, "ensure_models", None)
        if not callable(ensure):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop in sync tests
            return
        self._models_task = loop.create_task(self._prefetch_models(ensure))

    async def _prefetch_models(self, ensure: Any) -> None:
        try:
            await ensure("all")
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception as exc:  # pragma: no cover - downloader reports its own errors
            log.warning("model prefetch failed: %s", exc)
        # The local neural voice (Piper, ~63 MB) is what makes the assistant
        # stop sounding like a robot; it is fetched after the STT models so the
        # window becomes usable first.
        local = getattr(self._voice, "ensure_local_voice", None)
        if not callable(local):
            return
        try:
            await local()
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception as exc:  # pragma: no cover - never break startup
            log.warning("local voice prefetch failed: %s", exc)

    async def shutdown(self) -> None:
        self._started = False
        self._cancel_reset_timer()
        if self._models_task is not None:
            task, self._models_task = self._models_task, None
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            await self._voice.stop()
        except Exception as exc:  # pragma: no cover - defensive
            log.error("voice stop failed: %s", exc)
        try:
            await self._llm.aclose()
        except Exception as exc:  # pragma: no cover - defensive
            log.error("llm close failed: %s", exc)

    # ------------------------------------------------------------------ public API
    @property
    def config(self) -> ConfigManager:
        return self._config

    @property
    def apps(self) -> AppsRegistry:
        return self._apps

    @property
    def llm(self) -> CascadeClient:
        return self._llm

    @property
    def voice(self) -> VoicePipeline:
        return self._voice

    @property
    def paused(self) -> bool:
        return self._paused

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def last_user_text(self) -> str | None:
        return self._last_user_text

    async def handle_text(self, text: str, source: str = "window", replaced: str | None = None) -> str:
        """Process one utterance and return the spoken/written answer."""
        text = (text or "").strip()
        if not text:
            return ""

        async with self._command_lock:
            if source == "window":
                self._emit(transcript_event(text, "window"))
            self._last_user_text = text
            self._remember("user", text)
            self._arm_reset_timer()
            self._state.set(THINKING)

            # The restore must survive any failure below: THINKING is not a
            # state the wake path listens in (the pipeline gates on idle and
            # speaking), so a raised provider call used to leave the assistant
            # permanently deaf until the app was restarted.
            spoken = False
            try:
                answer = await self._respond(text)
                self._remember("assistant", answer)
                self._emit(answer_event(answer, replaced))

                await self._speak(answer)
                spoken = bool(answer.strip())
                return answer
            finally:
                self._restore_idle_state()
                # Voice conversations keep the microphone hot for a few
                # seconds: "а теперь подробнее" needs no wake word (v2 UX).
                if spoken and source == "voice" and not self._paused:
                    begin = getattr(self._voice, "begin_followup", None)
                    if callable(begin):
                        begin()

    async def repeat(self) -> str:
        """Re-run the last utterance (TZ §2.4): repair after a recognition error."""
        text = self._last_user_text
        if not text:
            return ""
        return await self.handle_text(text, source="repeat", replaced=text)

    async def listen(self) -> None:
        await self._voice.listen_now()

    async def stop_speaking(self) -> None:
        await self._voice.stop_speaking()

    async def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        if self._paused:
            await self._voice.pause()
            self._state.set(PAUSED)
        else:
            await self._voice.resume()
            self._state.set(IDLE)

    def reload_apps(self) -> dict[str, Any]:
        """Re-read apps.json after the settings editor saved it (TZ §11)."""
        self._apps.load()
        self._router = Router(self._config, self._apps)
        self._tools = ToolExecutor(self._config, self._apps)
        return self._apps.snapshot()

    def apply_config(self) -> None:
        """Apply config changes without restarting the sidecar (TZ §11)."""
        try:
            self._router = Router(self._config, self._apps)
            self._tools = ToolExecutor(self._config, self._apps)
            self._voice.apply_config()
            max_messages = int(self._config.get("llm.max_messages", 8) or 8)
            if max_messages != self._history.maxlen:
                self._history = deque(self._history, maxlen=max_messages)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("apply_config failed: %s", exc)

    # ------------------------------------------------------------------ dialogue
    async def _respond(self, text: str) -> str:
        router = self._router
        route = router.route(text)
        if route.tool:
            result = await self._tools.execute(route.tool, dict(route.args or {}))
            self._emit(tool_call_event(route.tool, dict(route.args or {}), result.ok))
            speech = (result.speech or "").strip()
            return speech or self._phrase("tool_refused_phrase")

        messages = self._llm_messages()
        result = await self._llm.complete(messages, tools=self._tools.specs())

        if result.error == "no_api_key":
            # A missing key is a setup problem, not a network failure: say so and
            # name the exact environment variables (TZ 7.2, owner report 2026-10-28).
            detail = str(getattr(result, "detail", "") or "")
            phrase = self._phrase("no_api_key_phrase")
            self._emit(error_event(f"{phrase} ({detail})" if detail else phrase, "no_api_key"))
            return phrase

        if result.error == "unauthorized":
            self._emit(error_event(self._phrase("auth_failed_phrase"), "auth"))
            return self._phrase("auth_failed_phrase")

        if result.error == "no_connection":
            self._emit(error_event(self._phrase("no_connection_phrase"), "llm"))
            return self._phrase("no_connection_phrase")

        if result.kind == "not_understood":
            return self._phrase("not_understood_phrase")

        if result.tool_calls:
            call = result.tool_calls[0] or {}
            name = str(call.get("name", ""))
            args = call.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            tool_result = await self._tools.execute(name, args)
            self._emit(tool_call_event(name, args, tool_result.ok))
            speech = (tool_result.speech or "").strip()
            return speech or self._phrase("tool_refused_phrase")

        answer = (result.text or "").strip()
        return answer or self._phrase("not_understood_phrase")

    def _llm_messages(self) -> list[dict[str, Any]]:
        system = build_system_prompt(self._config, self._apps)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(
            {"role": item["role"], "content": item["content"]}
            for item in self._history
            if item["role"] in {"user", "assistant"}
        )
        return messages

    def _remember(self, role: str, content: str) -> None:
        if not content:
            return
        self._history.append({"role": role, "content": content, "ts": time.time()})

    async def _speak(self, text: str) -> None:
        if not (text or "").strip():
            return
        try:
            await self._voice.speak(text)
        except Exception as exc:  # offline TTS must degrade to text-only (TZ §15)
            log.error("tts failed: %s", exc)

    # ------------------------------------------------------------------ timers
    def _arm_reset_timer(self) -> None:
        self._cancel_reset_timer()
        raw = self._config.get("llm.reset_timeout_sec", 120)
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            timeout = 120.0
        if timeout <= 0:
            timeout = 120.0
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop in sync unit tests
            return
        self._reset_task = loop.create_task(self._reset_history_after(timeout))

    async def _reset_history_after(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:  # pragma: no cover - cancellation is the normal path
            raise
        self._history.clear()
        self._last_user_text = None
        log.debug("dialogue window cleared after %.0f s of silence", timeout)

    def _cancel_reset_timer(self) -> None:
        if self._reset_task is not None and not self._reset_task.done():
            self._reset_task.cancel()
        self._reset_task = None

    def _restore_idle_state(self) -> None:
        self._state.set(PAUSED if self._paused else IDLE)

    # ------------------------------------------------------------------ helpers
    def _phrase(self, key: str) -> str:
        return str(self._config.get(f"errors.{key}", ""))

    async def _on_utterance(self, text: str) -> None:
        try:
            await self.handle_text(text, source="voice")
        except Exception as exc:  # pragma: no cover - defensive
            log.error("utterance handling failed: %s", exc)
            self._emit(error_event(self._phrase("no_connection_phrase"), "runtime"))

    def _on_config_change(self, _snapshot: dict[str, Any]) -> None:
        self.apply_config()
