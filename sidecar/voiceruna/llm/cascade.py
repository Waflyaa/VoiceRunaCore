"""Provider cascade for the LLM layer (TZ §7.3, docs/CONTRACTS.md).

Providers are tried in priority order. Transient failures move silently to the
next provider; a bad key (401) disables that provider until restart. When every
candidate is unavailable the caller gets LLMResult(error='no_connection'); when
the only reason is that no provider has an API key, the error is 'no_api_key'
and LLMResult.detail lists the missing environment variables in Russian.

No dialog content is logged: only provider ids, error kinds and statuses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from ..config import ConfigManager
from .adapter import DEFAULT_TIMEOUT_SEC, Completion, ProviderAdapter, ProviderError

logger = logging.getLogger(__name__)

# Providers on these hosts are local helpers (Ollama, LM Studio): they may run
# without any API key and must not receive an Authorization header.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1"})


@dataclass
class LLMResult:
    """Outcome of one cascade request (frozen contract)."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{'name','args'}]
    provider_id: str | None = None
    error: str | None = None  # 'no_connection' | 'no_api_key' | 'unauthorized' | None
    kind: str = "ok"  # 'ok' | 'not_understood'
    detail: str = ""  # human-readable reason for the window (e.g. missing keys)


TransportFactory = Callable[[str], httpx.AsyncBaseTransport | None]


def _missing_keys_detail(missing: list[tuple[str, str]]) -> str:
    """Russian list of providers skipped because their API key is empty."""
    parts = []
    for env_name, provider_id in missing:
        label = env_name or "api_key_env не задан"
        parts.append(f"{label} ({provider_id})")
    return "нет ключа: " + ", ".join(parts)


def _provider_error_message(exc: ProviderError) -> str:
    """Short Russian message for the settings window."""
    status = exc.status
    if exc.kind == "auth":
        if status == 403:
            # OpenRouter and friends answer 403 for blocked regions/IPs as well
            # as for invalid keys; the owner must know both possibilities.
            return "Доступ запрещён (403): ключ не принят или провайдер блокирует регион/сеть"
        return f"Ошибка ключа ({status or 401})"
    if exc.kind == "rate_limit":
        return "429 — лимит запросов"
    if exc.kind == "payment":
        return "402 — нужна оплата"
    if exc.kind == "timeout":
        return "Таймаут — провайдер не отвечает"
    if exc.kind == "network":
        return "Нет сети — провайдер недоступен"
    if exc.kind == "server":
        return f"{status or 500} — ошибка сервера"
    return "Некорректный ответ провайдера"


class CascadeClient:
    """Run one request through the configured providers until one answers."""

    def __init__(
        self,
        config: ConfigManager,
        *,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._config = config
        self._transport_factory = transport_factory
        self._adapters: dict[str, ProviderAdapter] = {}
        self._disabled: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    def _timeout(self) -> float:
        try:
            value = float(self._config.get("llm.timeout_sec", DEFAULT_TIMEOUT_SEC))
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT_SEC
        return value if value > 0 else DEFAULT_TIMEOUT_SEC

    def _api_key(self, provider: dict[str, Any]) -> str:
        name = str(provider.get("api_key_env") or "")
        if not name:
            return ""
        try:
            return str(self._config.env_value(name) or "")
        except Exception:
            return ""

    @staticmethod
    def _is_local(provider: dict[str, Any]) -> bool:
        """True when base_url points to this machine (key is optional there)."""
        base_url = str(provider.get("base_url") or "").strip()
        if not base_url:
            return False
        try:
            host = urlsplit(base_url).hostname or ""
        except ValueError:
            return False
        return host.lower() in _LOCAL_HOSTS

    def _provider_usable(self, provider: dict[str, Any]) -> bool:
        """A provider is usable with a key, or without one when it is local."""
        return bool(self._api_key(provider)) or self._is_local(provider)

    def _adapter(self, provider: dict[str, Any]) -> ProviderAdapter:
        """Return the cached adapter for a provider id, refreshing its entry."""
        provider_id = str(provider.get("id") or "")
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            transport = self._transport_factory(provider_id) if self._transport_factory else None
            adapter = ProviderAdapter(
                provider,
                self._config.env_value,
                timeout=self._timeout(),
                transport=transport,
            )
            self._adapters[provider_id] = adapter
        else:
            adapter.provider = dict(provider)
            adapter.timeout = self._timeout()
        return adapter

    # ------------------------------------------------------------------ public API
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        """Try providers in priority order and return the first usable answer."""
        candidates = self._config.enabled_providers(require_tools=bool(tools))
        usable = [p for p in candidates if str(p.get("id") or "") not in self._disabled]
        if not usable:
            logger.debug("llm cascade: no enabled providers left")
            return LLMResult(error="no_connection")

        attempted = False
        missing_keys: list[tuple[str, str]] = []  # (api_key_env, provider id)
        auth_errors: list[tuple[str, str]] = []  # (provider id, russian reason)
        non_auth_error = False
        for provider in usable:
            provider_id = str(provider.get("id") or "")
            if not provider_id:
                continue
            if not self._provider_usable(provider):
                env_name = str(provider.get("api_key_env") or "").strip()
                missing_keys.append((env_name, provider_id))
                logger.debug(
                    "llm cascade: provider %s has no api key (%s)", provider_id, env_name
                )
                continue
            attempted = True
            try:
                completion = await self._adapter(provider).complete(messages, tools)
            except ProviderError as exc:
                if exc.kind == "auth":
                    reason = _provider_error_message(exc)
                    self._disabled[provider_id] = reason
                    auth_errors.append((provider_id, reason))
                else:
                    non_auth_error = True
                logger.debug(
                    "llm cascade: provider %s failed: kind=%s status=%s",
                    provider_id,
                    exc.kind,
                    exc.status,
                )
                continue
            except Exception as exc:  # never leak into asyncio tasks
                logger.debug(
                    "llm cascade: provider %s crashed: %s",
                    provider_id,
                    exc.__class__.__name__,
                )
                continue
            return self._to_result(provider_id, completion)

        if not attempted and missing_keys:
            # Nothing was even attempted: the only reason is missing API keys.
            logger.debug("llm cascade: all providers have no api key")
            return LLMResult(error="no_api_key", detail=_missing_keys_detail(missing_keys))
        if auth_errors and not non_auth_error:
            # A rejected key is a setup problem: the owner must see it instead of
            # the generic "нет связи" (single-provider installs hit this always).
            detail = "; ".join(f"{pid}: {reason}" for pid, reason in auth_errors)
            logger.debug("llm cascade: every key was rejected: %s", detail)
            return LLMResult(error="unauthorized", detail=detail)
        logger.debug("llm cascade: every usable provider failed")
        return LLMResult(error="no_connection")

    @staticmethod
    def _to_result(provider_id: str, completion: Completion) -> LLMResult:
        text = completion.text or ""
        if completion.tool_call is not None:
            return LLMResult(
                text=text,
                tool_calls=[completion.tool_call],
                provider_id=provider_id,
                kind="ok",
            )
        if text.strip() == "NOT_UNDERSTOOD":
            # The runtime uses errors.not_understood_phrase from the config.
            return LLMResult(text="", provider_id=provider_id, kind="not_understood")
        return LLMResult(text=text, provider_id=provider_id, kind="ok")

    async def test_provider(self, provider_id: str) -> tuple[bool, str]:
        """Check one provider's key/model with a minimal request."""
        provider = self._config.provider(provider_id)
        if provider is None:
            return False, "Провайдер не найден"
        if not self._provider_usable(provider):
            env_name = str(provider.get("api_key_env") or "").strip()
            return False, (f"Ключ не задан: {env_name}" if env_name else "Ключ не задан")
        try:
            await self._adapter(provider).test()
        except ProviderError as exc:
            logger.debug(
                "llm test: provider %s failed: kind=%s status=%s",
                provider_id,
                exc.kind,
                exc.status,
            )
            return False, _provider_error_message(exc)
        except Exception as exc:
            logger.debug(
                "llm test: provider %s crashed: %s",
                provider_id,
                exc.__class__.__name__,
            )
            return False, "Не удалось проверить провайдера"
        # An explicit successful check proves the key works: let the cascade use
        # the provider again without waiting for a restart.
        self._disabled.pop(provider_id, None)
        return True, "Ключ работает"

    async def list_models(self, provider_id: str) -> list[str]:
        """Return provider model ids; an error yields an empty list."""
        provider = self._config.provider(provider_id)
        if provider is None:
            return []
        if not self._provider_usable(provider):
            logger.debug(
                "llm models: provider %s has no api key (%s)",
                provider_id,
                str(provider.get("api_key_env") or "").strip(),
            )
            return []
        try:
            return await self._adapter(provider).list_models()
        except ProviderError as exc:
            logger.debug(
                "llm models: provider %s failed: kind=%s status=%s",
                provider_id,
                exc.kind,
                exc.status,
            )
        except Exception as exc:
            logger.debug(
                "llm models: provider %s crashed: %s",
                provider_id,
                exc.__class__.__name__,
            )
        return []

    def disabled_ids(self) -> list[str]:
        """Providers disabled until restart because of a bad key."""
        return list(self._disabled)

    def disabled_reasons(self) -> dict[str, str]:
        """Provider id -> Russian reason shown in the window."""
        return dict(self._disabled)

    def reset(self) -> None:
        """Forget disabled providers (used after the user edits keys)."""
        self._disabled.clear()

    async def aclose(self) -> None:
        """Close every cached HTTP client."""
        adapters = list(self._adapters.values())
        self._adapters.clear()
        for adapter in adapters:
            try:
                await adapter.aclose()
            except Exception:
                pass
