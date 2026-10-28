# -*- coding: utf-8 -*-
"""Issue #58 超时阶段归因回归：首响应/空闲/响应墙钟/报告预算彼此可区分。

复用既有 loopback SDK 夹具（``tests.test_agent_stream_lifecycle.stream_env``），
真实 SDK 流式读取与真实 HTTP 连接释放，不 mock 被测窗口。
"""

from __future__ import annotations

import threading

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
    # 5s backstop armed at real response start, not arrangement time, so
    # admission/prep latency cannot consume it before the 0.8s cap is measured.
    state["endless_self_stop_seconds"] = 5.0
    run = ResearchRun(15.0)
    with run.bind():
        response = adapter.call_completion(_messages(), timeout=12)
    [attempt] = run.attempts
    _wait_server_idle(state)
    assert response.provider == "error"
    assert attempt.status == "timeout"
    assert attempt.timeout_phase == "response_total"

    state["endless"] = True
    state["endless_self_stop_seconds"] = 5.0
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


def test_call_budget_beats_configured_response_cap_and_releases_connection(stream_env):  # noqa: F811
    """调用自身预算(0.8s)小于配置响应上限(10s)并先到期：记 call_budget 并释放连接。

    持续 chunk 的 endless 流：归因不冒充配置的 response_total（调高响应上限
    无法修复该类失败），报告预算(15s)仍有余量，到期连接真实关闭。"""
    adapter, state = stream_env
    Config._instance.llm_stream_response_timeout = 10.0
    Config._instance.llm_stream_first_chunk_timeout = 3.0
    Config._instance.llm_stream_idle_chunk_timeout = 3.0
    state["endless"] = True
    state["endless_self_stop_seconds"] = 5.0
    run = ResearchRun(15.0)
    with run.bind():
        response = adapter.call_completion(_messages(), timeout=0.8)
    [attempt] = run.attempts
    _wait_server_idle(state)
    assert response.provider == "error"
    assert attempt.timeout_phase == "call_budget"
    assert state["active"] == 0, "the expired call budget must release the connection"


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


def test_response_cap_measured_after_real_admission_wait_beyond_fixture_guard(stream_env):  # noqa: F811
    """真实准入等待（持有唯一并发槽 5.4s）超过旧 5s 布置时刻自停守卫后，
    响应阶段开始时自锚定的自停仍让 0.8s 响应上限被真实测量：记
    response_total 并关闭连接。旧锚点下同样等待会以干净 [DONE] 无 finish
    结束流制造 parse_error 假失败（root-stream-fixture-clock.log 反例）。
    真实槽位/定时器/SDK，不 mock 准入、计时或传输。"""
    adapter, state = stream_env
    from src.llm import concurrency

    Config._instance.llm_stream_response_timeout = 0.8
    Config._instance.llm_stream_first_chunk_timeout = 3.0
    Config._instance.llm_stream_idle_chunk_timeout = 3.0
    slots = concurrency._get_slots()
    assert slots.acquire(blocking=False), "the fixture owns exactly one admission slot"
    released = threading.Event()

    def release() -> None:
        slots.release()
        released.set()

    state["endless"] = True
    # No absolute endless_deadline: the guard anchors itself when the handler
    # starts the endless phase, so this 5s window begins at real response
    # start even though the call was admitted 5.4s later.
    state["endless_self_stop_seconds"] = 5.0
    timer = threading.Timer(5.4, release)
    timer.start()
    run = ResearchRun(15.0)
    try:
        with run.bind():
            response = adapter.call_completion(_messages(), timeout=12)
    finally:
        timer.join(timeout=7)
    assert released.is_set(), "the real admission slot must be released"
    [attempt] = run.attempts
    # The model call itself queued beyond the old arrangement-time guard; a
    # released-but-never-waited call would not prove the anchor under late
    # handler entry. 模型调用本身排队超过旧守卫时长，而非仅计时器最终释放。
    assert attempt.queue_seconds is not None and attempt.queue_seconds > 5.0
    _wait_server_idle(state)
    assert response.provider == "error"
    assert attempt.status == "timeout"
    assert attempt.timeout_phase == "response_total"
    assert state["active"] == 0, "the expired response cap must release the connection"
