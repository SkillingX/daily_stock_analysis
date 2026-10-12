from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from src.analyzer import GeminiAnalyzer


def test_call_litellm_uses_configured_response_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("LLM_USAGE_HMAC_KEY_VERSION", "test-v1")
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._router = None
    analyzer._config_override = SimpleNamespace(
        litellm_model="openai/test-model",
        litellm_fallback_models=[],
        llm_model_list=[],
        llm_temperature=0.7,
        openai_api_keys=["test-api-key"],
        openai_base_url=None,
        gemini_api_keys=[],
        anthropic_api_keys=[],
        deepseek_api_keys=[],
        llm_stream_response_timeout=23.0,
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    captured: dict[str, Any] = {}

    def fake_dispatch(_model: str, call_kwargs: dict[str, Any], **_kwargs: Any) -> Any:
        captured.update(call_kwargs)
        return response

    with patch.object(analyzer, "_dispatch_litellm_completion", side_effect=fake_dispatch):
        text, _, _ = analyzer._call_litellm(
            "market context", {"max_tokens": 8192, "temperature": 0.7}
        )

    assert text == "ok"
    assert captured["timeout"] == 23.0
