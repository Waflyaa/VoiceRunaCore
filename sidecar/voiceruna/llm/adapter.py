"""OpenAI-compatible provider adapter (TZ §7.1).

Any service speaking the OpenAI chat completions protocol (OpenRouter, DeepSeek,
OpenAI, Groq, Together, a local Ollama or LM Studio) is usable through config
alone, without touching this code.

The API key is never stored in config.json: it is resolved through an injected
env getter (ConfigManager.env_value) on every request.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 20.0
TEMPERATURE = 0.3

_OPENROUTER_MARKER = "openrouter.ai"
_REFERER = "https://github.com/Waflyaa/VoiceRunaCore"
_TITLE = "VoiceRunaCore"


@dataclass
class Completion:
    """Normalized chat completion: plain text plus at most one tool call."""

    text: str = ""
    tool_call: dict[str, Any] | None = None  # {'name': str, 'args': dict}


class ProviderError(Exception):
    """A provider request failed; ``kind`` drives the cascade decision."""

    def __init__(self, message: str, *, status: int | None = None, kind: str = "server") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind


def _error_kind(status: int) -> str:
    """Map an HTTP status to a cascade error kind."""
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status == 402:
        return "payment"
    if status >= 500:
        return "server"
    return "bad_response"


class ProviderAdapter:
    """One HTTP client for one OpenAI-compatible provider entry."""

    def __init__(
        self,
        provider: dict[str, Any],
        env_getter: Callable[[str], str],
        timeout: float = DEFAULT_TIMEOUT_SEC,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider: dict[str, Any] = dict(provider or {})
        self._env_getter = env_getter
        self.timeout = float(timeout or DEFAULT_TIMEOUT_SEC)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ client
    def _http(self) -> httpx.AsyncClient:
        """Return the single client belonging to this adapter (created lazily)."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client (idempotent)."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # ------------------------------------------------------------------ helpers
    def _model(self) -> str:
        return str(self.provider.get("model") or "")

    def _api_key(self) -> str:
        name = str(self.provider.get("api_key_env") or "")
        if not name:
            return ""
        try:
            return str(self._env_getter(name) or "")
        except Exception:  # a broken .env must not crash a request
            return ""

    def _url(self, path: str) -> str:
        base = str(self.provider.get("base_url") or "").strip().rstrip("/")
        if not base:
            raise ProviderError("provider base_url is empty", kind="bad_response")
        return f"{base}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if _OPENROUTER_MARKER in str(self.provider.get("base_url") or "").lower():
            headers["HTTP-Referer"] = _REFERER
            headers["X-Title"] = _TITLE
        return headers

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """Short human-readable provider error; never includes dialog text."""
        try:
            data = response.json()
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:200]
        if isinstance(error, str):
            return error[:200]
        message = data.get("message")
        if isinstance(message, str):
            return message[:200]
        return ""

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        detail = self._error_detail(response)
        message = f"HTTP {status}"
        if detail:
            message = f"{message}: {detail}"
        raise ProviderError(message, status=status, kind=_error_kind(status))

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                "provider returned a non-JSON response",
                status=response.status_code,
                kind="bad_response",
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(
                "provider returned an unexpected JSON payload",
                status=response.status_code,
                kind="bad_response",
            )
        return data

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform one HTTP call, translating transport failures to ProviderError."""
        url = self._url(path)
        try:
            response = await self._http().request(
                method,
                url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("provider request timed out", kind="timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"provider network error: {exc.__class__.__name__}",
                kind="network",
            ) from exc
        self._raise_for_status(response)
        return response

    # ------------------------------------------------------------------ API
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        """POST one chat completion and normalize the reply (TZ §7.4)."""
        payload: dict[str, Any] = {
            "model": self._model(),
            "messages": messages,
            "temperature": TEMPERATURE,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        response = await self._request("POST", "chat/completions", payload)
        data = self._json(response)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError(
                "provider response has no choices",
                status=response.status_code,
                kind="bad_response",
            )
        message = choices[0].get("message")
        if not isinstance(message, dict):
            message = {}

        content = message.get("content")
        text = content if isinstance(content, str) else ""

        tool_call: dict[str, Any] | None = None
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list) and raw_calls:
            # Only the first tool call is executed (TZ §7.4, mandatory).
            tool_call = self._parse_tool_call(raw_calls[0])
        return Completion(text=text, tool_call=tool_call)

    @staticmethod
    def _parse_tool_call(raw: Any) -> dict[str, Any] | None:
        """Convert an OpenAI tool call to {'name', 'args'}; None when unusable."""
        if not isinstance(raw, dict):
            return None
        function = raw.get("function")
        if not isinstance(function, dict):
            return None
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        raw_args = function.get("arguments")
        if isinstance(raw_args, dict):
            args: Any = raw_args
        elif isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, ValueError):
                return None
        else:
            return None
        if not isinstance(args, dict):
            return None
        return {"name": name, "args": args}

    async def list_models(self) -> list[str]:
        """GET {base_url}/models and return the model ids."""
        response = await self._request("GET", "models")
        data = self._json(response)
        rows = data.get("data")
        if not isinstance(rows, list):
            raise ProviderError(
                "provider /models response has no data list",
                status=response.status_code,
                kind="bad_response",
            )
        models: list[str] = []
        for row in rows:
            if isinstance(row, dict):
                model_id = row.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    models.append(model_id)
        return models

    async def test(self) -> None:
        """Cheap key check: ping with max_tokens=1; raises ProviderError on failure."""
        await self._request(
            "POST",
            "chat/completions",
            {
                "model": self._model(),
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
        )
