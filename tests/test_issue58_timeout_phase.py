# -*- coding: utf-8 -*-
"""Issue #58 超时阶段归因回归：首响应/空闲/响应墙钟/报告预算彼此可区分。

复用既有 loopback SDK 夹具（``tests.test_agent_stream_lifecycle.stream_env``），
真实 SDK 流式读取与真实 HTTP 连接释放，不 mock 被测窗口。
"""

from __future__ import annotations

import time

import pytest

from src.config import Config
from src.core.research_run import ResearchRun, ResearchStopped
from tests.test_agent_stream_lifecycle import _messages, _ok_script, _wait_server_idle, stream_env  # noqa: F401


def test_response_wall_clock_and_report_budget_stay_distinguishable(stream_env):  # noqa: F811
    """响应墙钟到顶记 response_total；报告预算到顶记 report_budget，不互相冒充。"""
    adapter, state = stream_env
    Config._instance.llm_stream_response_timeout = 0.8
    Config._instance.llm_stream_first_chunk_timeout = 3.0
    Config._instance.llm_stream_idle_chunk_timeout = 3.0
    state["endless"] = True
    state["endless_deadline"] = time.monotonic() + 5.0
    run = ResearchRun(15.0)
    with run.bind():
        response = adapter.call_completion(_messages(), timeout=12)
    [attempt] = run.attempts
    _wait_server_idle(state)
    assert response.provider == "error"
    assert attempt.status == "timeout"
    assert attempt.timeout_phase == "response_total"

    state["endless"] = True
    state["endless_deadline"] = time.monotonic() + 5.0
    exhausted = ResearchRun(0.8)
    with exhausted.bind():
        response = adapter.call_completion(_messages(), timeout=12)
    [attempt] = exhausted.attempts
    _wait_server_idle(state)
    assert response.provider == "error"
    assert attempt.timeout_phase == "report_budget"
    with pytest.raises(ResearchStopped):
        exhausted.check()
    assert exhausted.stop_reason == "report_budget"


@pytest.mark.parametrize("window", ["first_response", "idle_chunk"])
def test_first_response_and_idle_windows_are_distinct_causes(stream_env, window):  # noqa: F811
    """首响应（响应头+首 chunk 共用窗口）与后续 chunk 空闲分别归因。"""
    adapter, state = stream_env
    Config._instance.llm_stream_response_timeout = 10.0
    Config._instance.llm_stream_first_chunk_timeout = 0.6 if window == "first_response" else 3.0
    Config._instance.llm_stream_idle_chunk_timeout = 0.6
    state["script"] = _ok_script()
    state["script"][0 if window == "first_response" else 1]["delay"] = 1.2
    run = ResearchRun(15)
    with run.bind():
        response = adapter.call_completion(_messages(), timeout=12)
    [attempt] = run.attempts
    _wait_server_idle(state)
    assert response.provider == "error"
    assert attempt.status == "timeout"
    assert attempt.timeout_phase == window
    assert state["active"] == 0, "the expired window must release the connection"
