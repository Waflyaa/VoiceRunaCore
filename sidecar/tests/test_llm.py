"""Tests for the LLM layer: adapter, cascade and prompt.

Every HTTP call is served by httpx.MockTransport, so the suite never touches
the network and never needs real API keys.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

from voiceruna.llm import (
    CascadeClient,
    LLMResult,
    ProviderAdapter,
    ProviderError,
    build_system_prompt,
)

MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "привет"}]
TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}
]


def make_provider(
    pid: str,
    *,
    priority: int = 1,
    supports_tools: bool = True,
    enabled: bool = True,
) -> dict[str, Any]:
    """A provider entry with a fake base_url and a test-only key name."""
    return {
        "id": pid,
        "enabled": enabled,
        "base_url": f"https://{pid}.test/v1",
        "api_key_env": f"{pid.upper()}_API_KEY",
        "model": f"{pid}-model",
        "supports_tools": supports_tools,
        "priority": priority,
        "label": pid.title(),
    }


def use_providers(config, providers: list[dict[str, Any]], *, keys: bool = True) -> None:
    config.set("llm.providers", providers)
    if keys:
        for provider in providers:
            config.set_env_key(provider["api_key_env"], f"key-{provider['id']}")


def chat_response(
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": message}]})


def tool_call(name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def build_client(
    config,
    handlers: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> CascadeClient:
    transports = {pid: httpx.MockTransport(handler) for pid, handler in handlers.items()}
    return CascadeClient(config, transport_factory=lambda pid: transports.get(pid))


# ---------------------------------------------------------------- cascade basics
async def test_cascade_falls_through_rate_limit(config):
    use_providers(config, [make_provider("first", priority=1), make_provider("second", priority=2)])
    requests: list[httpx.Request] = []

    def first(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    def second(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return chat_response("Привет")

    client = build_client(config, {"first": first, "second": second})
    try:
        result = await client.complete(MESSAGES)
    finally:
        await client.aclose()

    assert isinstance(result, LLMResult)
    assert result.error is None
    assert result.kind == "ok"
    assert result.text == "Привет"
    assert result.provider_id == "second"
    assert client.disabled_ids() == []

    first_request = requests[0]
    assert str(first_request.url) == "https://first.test/v1/chat/completions"
    assert first_request.headers["authorization"] == "Bearer key-first"
    body = json.loads(first_request.content)
    assert body["model"] == "first-model"
    assert body["temperature"] == 0.3
    assert body["stream"] is False
    assert "tools" not in body


@pytest.mark.parametrize("failure", ["402", "500", "timeout"])
async def test_cascade_falls_through_transient_failures(config, failure):
    use_providers(config, [make_provider("first", priority=1), make_provider("second", priority=2)])

    def first(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("provider is slow", request=request)
        return httpx.Response(int(failure), json={"error": {"message": "failure"}})

    def second(request: httpx.Request) -> httpx.Response:
        return chat_response("ok")

    client = build_client(config, {"first": first, "second": second})
    try:
        result = await client.complete(MESSAGES)
    finally:
        await client.aclose()

    assert result.error is None
    assert result.text == "ok"
    assert result.provider_id == "second"
    assert client.disabled_ids() == []


async def test_auth_error_disables_provider_until_reset(config):
    use_providers(config, [make_provider("first", priority=1), make_provider("second", priority=2)])
    first_calls: list[httpx.Request] = []

    def first(request: httpx.Request) -> httpx.Response:
        first_calls.append(request)
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    def second(request: httpx.Request) -> httpx.Response:
        return chat_response("answer")

    client = build_client(config, {"first": first, "second": second})
    try:
        result = await client.complete(MESSAGES)
        assert result.provider_id == "second"
        assert "first" in client.disabled_ids()
        assert "first" in client.disabled_reasons()

        again = await client.complete(MESSAGES)
        assert again.provider_id == "second"
        assert len(first_calls) == 1

        client.reset()
        assert client.disabled_ids() == []
        await client.complete(MESSAGES)
        assert len(first_calls) == 2
    finally:
        await client.aclose()


async def test_reset_after_auth_error_restores_provider(config):
    use_providers(config, [make_provider("first", priority=1), make_provider("second", priority=2)])
    first_calls = 0

    def first(request: httpx.Request) -> httpx.Response:
        nonlocal first_calls
        first_calls += 1
        if first_calls == 1:
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        return chat_response("первый снова работает")

    def second(request: httpx.Request) -> httpx.Response:
        return chat_response("второй")

    client = build_client(config, {"first": first, "second": second})
    try:
        first_result = await client.complete(MESSAGES)
        assert first_result.provider_id == "second"
        assert "first" in client.disabled_ids()

        client.reset()

        restored = await client.complete(MESSAGES)
        assert restored.provider_id == "first"
        assert restored.text == "первый снова работает"
        assert client.disabled_ids() == []
        assert first_calls == 2
    finally:
        await client.aclose()


async def test_all_providers_fail_returns_no_connection(config):
    use_providers(config, [make_provider("first", priority=1), make_provider("second", priority=2)])

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    client = build_client(config, {"first": broken, "second": broken})
    try:
        result = await client.complete(MESSAGES)
    finally:
        await client.aclose()

    assert result.error == "no_connection"
    assert result.text == ""
    assert result.tool_calls == []
    assert result.provider_id is None


async def test_missing_key_returns_no_api_key_without_requests(config, monkeypatch):
    monkeypatch.delenv("UNKEYED_API_KEY", raising=False)
    use_providers(config, [make_provider("unkeyed")], keys=False)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return chat_response("should not happen")

    client = build_client(config, {"unkeyed": handler})
    try:
        result = await client.complete(MESSAGES)
    finally:
        await client.aclose()

    assert result.error == "no_api_key"
    assert result.detail == "нет ключа: UNKEYED_API_KEY (unkeyed)"
    assert result.provider_id is None
    assert requests == []


async def test_all_providers_without_keys_list_env_names(config, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    use_providers(
        config,
        [make_provider("openrouter", priority=1), make_provider("deepseek", priority=2)],
        keys=False,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return chat_response("should not happen")

    client = build_client(config, {"openrouter": handler, "deepseek": handler})
    try:
        result = await client.complete(MESSAGES)
    finally:
        await client.aclose()

    assert result.error == "no_api_key"
    assert result.detail == (
        "нет ключа: OPENROUTER_API_KEY (openrouter), DEEPSEEK_API_KEY (deepseek)"
    )
    assert requests == []


async def test_missing_key_plus_network_failure_is_no_connection(config, monkeypatch):
    monkeypatch.delenv("NOKEY_API_KEY", raising=False)
    use_providers(
        config,
        [make_provider("nokey", priority=1), make_provider("broken", priority=2)],
        keys=False,
    )
    config.set_env_key("BROKEN_API_KEY", "key-broken")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"error": {"message": "boom"}})

    client = build_client(config, {"nokey": handler, "broken": handler})
    try:
        result = await client.complete(MESSAGES)
    finally:
        await client.aclose()

    assert result.error == "no_connection"
    assert result.detail == ""
    assert len(requests) == 1  # only the provider that has a key was tried


async def test_local_provider_without_key_is_usable(config, monkeypatch):
    monkeypatch.delenv("OLLAMA_KEY", raising=False)
    local = {
        "id": "ollama",
        "enabled": True,
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": "OLLAMA_KEY",
        "model": "llama3",
        "supports_tools": True,
        "priority": 1,
        "label": "Ollama",
    }
    use_providers(config, [local], keys=False)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return chat_response("локальный ответ")

    client = build_client(config, {"ollama": handler})
    try:
        result = await client.complete(MESSAGES)
    finally:
        await client.aclose()

    assert result.error is None
    assert result.kind == "ok"
    assert result.text == "локальный ответ"
    assert result.provider_id == "ollama"
    assert len(requests) == 1
    assert "authorization" not in requests[0].headers


async def test_no_enabled_providers_is_no_connection(config):
    use_providers(config, [make_provider("off", enabled=False)])
    client = build_client(config, {})
    try:
        result = await client.complete(MESSAGES)
    finally:
        await client.aclose()
    assert result.error == "no_connection"


# ---------------------------------------------------------------- tool calls
async def test_only_first_tool_call_is_kept(config):
    use_providers(config, [make_provider("only")])
    calls = [
        tool_call("launch_application", '{"application_id": "dota2"}'),
        tool_call("open_folder", '{"folder_id": "downloads"}'),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response("", calls)

    client = build_client(config, {"only": handler})
    try:
        result = await client.complete(MESSAGES, TOOLS)
    finally:
        await client.aclose()

    assert result.kind == "ok"
    assert result.provider_id == "only"
    assert result.tool_calls == [
        {"name": "launch_application", "args": {"application_id": "dota2"}}
    ]


async def test_broken_tool_arguments_fall_back_to_text(config):
    use_providers(config, [make_provider("only")])

    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response("Не получилось", [tool_call("launch_application", "{not json")])

    client = build_client(config, {"only": handler})
    try:
        result = await client.complete(MESSAGES, TOOLS)
    finally:
        await client.aclose()

    assert result.tool_calls == []
    assert result.text == "Не получилось"
    assert result.kind == "ok"


async def test_not_understood_marker(config):
    use_providers(config, [make_provider("only")])

    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response("  NOT_UNDERSTOOD  ")

    client = build_client(config, {"only": handler})
    try:
        result = await client.complete(MESSAGES, TOOLS)
    finally:
        await client.aclose()

    assert result.kind == "not_understood"
    assert result.text == ""
    assert result.provider_id == "only"
    assert result.tool_calls == []


# ---------------------------------------------------------------- providers
async def test_supports_tools_false_is_skipped_only_with_tools(config):
    use_providers(
        config,
        [
            make_provider("textonly", priority=1, supports_tools=False),
            make_provider("full", priority=2),
        ],
    )
    bodies: list[dict[str, Any]] = []

    def text_handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return chat_response("text answer")

    def full_handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return chat_response("tool answer")

    client = build_client(config, {"textonly": text_handler, "full": full_handler})
    try:
        with_tools = await client.complete(MESSAGES, TOOLS)
        assert with_tools.provider_id == "full"
        assert bodies[-1]["tools"] == TOOLS

        without_tools = await client.complete(MESSAGES)
        assert without_tools.provider_id == "textonly"
        assert "tools" not in bodies[-1]
    finally:
        await client.aclose()


# ---------------------------------------------------------------- test_provider
async def test_test_provider_without_key_does_not_call_network(config, monkeypatch):
    monkeypatch.delenv("UNKEYED_API_KEY", raising=False)
    use_providers(config, [make_provider("unkeyed")], keys=False)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return chat_response("pong")

    client = build_client(config, {"unkeyed": handler})
    try:
        ok, message = await client.test_provider("unkeyed")
    finally:
        await client.aclose()

    assert ok is False
    assert message == "Ключ не задан: UNKEYED_API_KEY"
    assert requests == []


async def test_test_provider_ok(config):
    use_providers(config, [make_provider("only")])
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return chat_response("pong")

    client = build_client(config, {"only": handler})
    try:
        ok, message = await client.test_provider("only")
    finally:
        await client.aclose()

    assert ok is True
    assert message == "Ключ работает"
    assert captured["body"]["max_tokens"] == 1
    assert captured["body"]["messages"][0]["content"] == "ping"


async def test_successful_test_provider_reenables_disabled_provider(config):
    use_providers(config, [make_provider("first", priority=1), make_provider("second", priority=2)])

    def first(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("max_tokens") == 1:  # test_provider ping after the key was fixed
            return chat_response("pong")
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    def second(request: httpx.Request) -> httpx.Response:
        return chat_response("ok")

    client = build_client(config, {"first": first, "second": second})
    try:
        await client.complete(MESSAGES)
        assert "first" in client.disabled_ids()

        ok, message = await client.test_provider("first")
        assert ok is True
        assert message == "Ключ работает"
        assert "first" not in client.disabled_ids()
    finally:
        await client.aclose()


async def test_test_provider_reports_auth_error(config):
    use_providers(config, [make_provider("only")])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = build_client(config, {"only": handler})
    try:
        ok, message = await client.test_provider("only")
    finally:
        await client.aclose()

    assert ok is False
    assert "401" in message
    assert "ключ" in message.lower()


async def test_test_provider_unknown_id(config):
    client = CascadeClient(config)
    try:
        ok, message = await client.test_provider("nope")
    finally:
        await client.aclose()
    assert ok is False
    assert message == "Провайдер не найден"


# ---------------------------------------------------------------- list_models
async def test_list_models(config):
    use_providers(config, [make_provider("only")])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://only.test/v1/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "model-a"}, {"id": "model-b"}, {"nope": 1}]},
        )

    client = build_client(config, {"only": handler})
    try:
        models = await client.list_models("only")
    finally:
        await client.aclose()

    assert models == ["model-a", "model-b"]


async def test_list_models_without_key_returns_empty_without_request(config, monkeypatch):
    monkeypatch.delenv("UNKEYED_API_KEY", raising=False)
    use_providers(config, [make_provider("unkeyed")], keys=False)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "should-not-happen"}]})

    client = build_client(config, {"unkeyed": handler})
    try:
        models = await client.list_models("unkeyed")
    finally:
        await client.aclose()

    assert models == []
    assert requests == []


async def test_list_models_error_returns_empty_list(config):
    use_providers(config, [make_provider("only")])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    client = build_client(config, {"only": handler})
    try:
        models = await client.list_models("only")
    finally:
        await client.aclose()

    assert models == []


# ---------------------------------------------------------------- adapter
async def test_adapter_maps_http_errors(config):
    provider = make_provider("mapped")
    cases = {400: "bad_response", 401: "auth", 402: "payment", 429: "rate_limit", 500: "server"}

    for status, kind in cases.items():
        def handler(request: httpx.Request, status: int = status) -> httpx.Response:
            return httpx.Response(status, json={"error": {"message": "provider failure"}})

        adapter = ProviderAdapter(
            provider,
            lambda name: "key",
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(ProviderError) as info:
                await adapter.complete(MESSAGES)
        finally:
            await adapter.aclose()
        assert info.value.status == status
        assert info.value.kind == kind

    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    adapter = ProviderAdapter(
        provider,
        lambda name: "key",
        transport=httpx.MockTransport(network_failure),
    )
    try:
        with pytest.raises(ProviderError) as info:
            await adapter.complete(MESSAGES)
    finally:
        await adapter.aclose()
    assert info.value.kind == "network"


# ---------------------------------------------------------------- prompt
class FakeApps:
    def describe_for_prompt(self) -> str:
        return "dota2 (Dota 2), factorio (Factorio), calc (Калькулятор)"


def test_build_system_prompt(config):
    config.set("weather.default_city", "Сургут")
    config.set("llm.system_prompt_extra", "Отвечай дружелюбно.")

    prompt = build_system_prompt(config, FakeApps())

    assert prompt.startswith("Ты — Вокс")
    assert "Я пока не умею это делать" in prompt
    assert "не пиши" in prompt and "NOT_UNDERSTOOD" in prompt
    assert "отвечаешь на ЛЮБЫЕ вопросы" in prompt
    assert "юмором" in prompt
    assert "launch_application" in prompt
    assert "dota2 (Dota 2)" in prompt
    assert "factorio (Factorio)" in prompt
    assert "Город по умолчанию: Сургут" in prompt
    assert prompt.rstrip().endswith("Отвечай дружелюбно.")
    assert "**" not in prompt


def test_system_prompt_uses_the_configured_assistant_name(config):
    config.set("wakeword.phrase", "руна")

    prompt = build_system_prompt(config, FakeApps())

    assert prompt.startswith("Ты — Руна")
    assert "Вокс" not in prompt


def test_single_provider_rejected_key_is_reported_as_unauthorized(config, monkeypatch):
    provider = make_provider("deepseek")
    use_providers(config, [provider])
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-invalid")
    client = CascadeClient(config, transport_factory=lambda pid: httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": {"message": "Authentication Fails"}})
    ))

    result = asyncio.run(client.complete(MESSAGES))

    assert result.error == "unauthorized"
    assert "deepseek" in result.detail
    assert "401" in result.detail
    asyncio.run(client.aclose())


def test_rejected_key_plus_server_error_stays_no_connection(config, monkeypatch):
    first = make_provider("alpha", priority=1)
    second = make_provider("beta", priority=2)
    use_providers(config, [first, second])
    monkeypatch.setenv("ALPHA_API_KEY", "a")
    monkeypatch.setenv("BETA_API_KEY", "b")

    def handler(request):
        if "alpha" in str(request.url):
            return httpx.Response(401, json={"error": "bad key"})
        return httpx.Response(500, json={"error": "boom"})

    client = CascadeClient(config, transport_factory=lambda pid: httpx.MockTransport(handler))
    result = asyncio.run(client.complete(MESSAGES))

    assert result.error == "no_connection"
    asyncio.run(client.aclose())


def test_blocked_region_403_is_reported_as_unauthorized(config, monkeypatch):
    provider = make_provider("openrouter")
    use_providers(config, [provider])
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    client = CascadeClient(config, transport_factory=lambda pid: httpx.MockTransport(
        lambda request: httpx.Response(403, json={"error": "Access denied by security policy."})
    ))

    result = asyncio.run(client.complete(MESSAGES))

    assert result.error == "unauthorized"
    assert "регион" in result.detail
    asyncio.run(client.aclose())
