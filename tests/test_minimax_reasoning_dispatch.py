from __future__ import annotations

from typing import Any

import pytest

from src import analyzer as analyzer_module
from src.analyzer import GeminiAnalyzer
from src.config import Config


def test_legacy_openai_model_default_uses_minimax_m31_flash() -> None:
    assert Config().openai_model == "MiniMax-M3.1-Flash"


@pytest.mark.parametrize(
    ("model", "expected_effort"),
    [
        ("openai/MiniMax-M3.1-Flash", "low"),
        ("openai/glm-5.3", None),
    ],
)
def test_analyzer_dispatch_applies_reasoning_only_to_minimax_m31(
    model: str,
    expected_effort: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return kwargs

    def immediate_completion(completion: Any, **kwargs: Any) -> Any:
        return completion(**kwargs)

    monkeypatch.setattr(analyzer_module.litellm, "completion", fake_completion)
    monkeypatch.setattr(analyzer_module, "limited_completion", immediate_completion)

    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._router = None
    config = Config(
        litellm_model=model,
        minimax_reasoning_effort="low",
        openai_api_keys=[],
    )
    analyzer._dispatch_litellm_completion(
        model,
        {"model": model, "messages": []},
        config=config,
        use_channel_router=False,
        router_model_names=set(),
    )

    if expected_effort is None:
        assert "extra_body" not in captured
    else:
        assert captured["extra_body"] == {
            "thinking": {"type": "adaptive"},
            "reasoning_effort": expected_effort,
        }
