"""A 403 must be explained precisely: blocked region/network or rejected key.

The owner hit exactly this: OpenRouter answers 403 "Access denied by security
policy" for every endpoint from his network, even with a valid-looking key.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from voiceruna.llm import ProviderError
from voiceruna.llm.cascade import CascadeClient, _provider_error_message


class _Config:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)

    def enabled_providers(self, require_tools: bool = False):
        return self._values.get("llm", {}).get("providers", [])

    def provider(self, provider_id: str):
        for item in self._values.get("llm", {}).get("providers", []):
            if item.get("id") == provider_id:
                return item
        return None

    def env_value(self, name: str) -> str:
        return self._values.get("env", {}).get(name, "")


def test_403_message_mentions_region_and_key():
    message = _provider_error_message(ProviderError("forbidden", status=403, kind="auth"))
    assert "403" in message
    assert "регион" in message


def test_401_message_stays_about_the_key():
    message = _provider_error_message(ProviderError("unauthorized", status=401, kind="auth"))
    assert "401" in message
    assert "регион" not in message


def test_test_provider_surfaces_the_403_reason():
    provider = {
        "id": "openrouter",
        "enabled": True,
        "base_url": "https://openrouter.test/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "test-model",
        "supports_tools": True,
        "priority": 1,
    }
    config = _Config({"llm": {"providers": [provider], "timeout_sec": 5}, "env": {"OPENROUTER_API_KEY": "sk-test"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "Access denied by security policy."})

    client = CascadeClient(config, transport_factory=lambda url: httpx.MockTransport(handler))
    ok, message = asyncio.run(client.test_provider("openrouter"))

    assert ok is False
    assert "403" in message
    assert "регион" in message
    asyncio.run(client.aclose())
