# -*- coding: utf-8 -*-
"""共享 LLM adapter 边界（issue #83 唯一批准的共享变更）+ 真实综合链路。

- ``propagate_program_errors=True``：终端 TypeError/AssertionError 原样上抛
  （真实 LLMToolAdapter.call_text 终端响应处触发），不伪装 provider="error"
- 默认 False：同一终端错误仍返回错误响应（全部既有消费者行为逐字节不变）
- 纯合成配置的 configured/unconfigured 显式断言（真实 adapter 的 bool
  property；不静默跳过、不 patch 不存在的名字）
- 合成 wire 经真实 ``httpx.AsyncClient.send`` 终端（LiteLLM 流协议全链路）
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import httpx
import pytest

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


def _configure_synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """受控合成 LLM 配置（无真实凭据；Config 单例 reset）。"""
    for key in list(os.environ):
        if any(t in key for t in ("ANTHROPIC_API", "GEMINI_API", "MINIMAX_API",
                                  "DEEPSEEK_API", "ZHIPU_API", "LLM_CHANNELS")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LITELLM_MODEL", "openai/synthetic-standard")
    monkeypatch.setenv("OPENAI_API_KEYS", "synthetic-placeholder")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from src.config import Config

    Config.reset_instance()


def _install_synthetic_wire(content: str):
    """patch httpx.AsyncClient.send 返回合成 SSE（LiteLLM 流协议真实）。"""
    import litellm  # noqa: F401  本地价格表开关在 import 期生效

    chunks = [
        {"id": "s", "object": "chat.completion.chunk", "created": 0,
         "model": "synthetic-standard",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": content},
                      "finish_reason": None}]},
        {"id": "s", "object": "chat.completion.chunk", "created": 0,
         "model": "synthetic-standard",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    wire = "".join(
        "data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks
    ) + "data: [DONE]\n\n"
    orig_send = httpx.AsyncClient.send

    async def _synthetic_send(client_self, request, *args, **kwargs):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=wire.encode(), request=request)

    return patch.object(httpx.AsyncClient, "send", _synthetic_send), orig_send


_CONTENT = json.dumps({"claims": [{"kind": "interpretation", "text": "政策构成催化",
                                   "evidence_refs": ["cat-1"]}]}, ensure_ascii=False)


class TestAdapterAvailability:
    """真实契约：bool property；configured/unconfigured 显式断言。"""

    def test_unconfigured_adapter_not_available(self, monkeypatch):
        monkeypatch.delenv("LITELLM_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEYS", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from src.config import Config

        Config.reset_instance()
        from src.agent.llm_adapter import LLMToolAdapter

        adapter = LLMToolAdapter(Config())
        assert adapter.is_available is False

    def test_configured_adapter_available_and_call_ok(self, monkeypatch):
        _configure_synthetic(monkeypatch)
        from src.agent.llm_adapter import LLMToolAdapter

        adapter = LLMToolAdapter()
        assert isinstance(adapter.is_available, bool)
        assert adapter.is_available is True
        wire, _ = _install_synthetic_wire(_CONTENT)
        with wire:
            resp = adapter.call_text([{"role": "user", "content": "hi"}], timeout=20)
        assert resp.provider != "error"
        assert resp.content == _CONTENT


class TestProgramErrorPropagation:
    """opt-in propagate_program_errors：终端程序错误上抛；默认行为不变。"""

    @pytest.fixture()
    def adapter(self, monkeypatch):
        _configure_synthetic(monkeypatch)
        from src.agent.llm_adapter import LLMToolAdapter

        return LLMToolAdapter()

    @pytest.mark.parametrize("exc", [
        TypeError("synthetic programming error"),
        AssertionError("synthetic invariant violated"),
    ])
    def test_optin_propagates_program_error(self, adapter, exc):
        messages: List[Dict[str, Any]] = [{"role": "user", "content": "hi"}]
        with patch.object(
            adapter, "_call_litellm_model", side_effect=exc,
        ):
            with pytest.raises(type(exc)):
                adapter.call_text(messages, timeout=10, propagate_program_errors=True)

    @pytest.mark.parametrize("exc", [
        TypeError("synthetic programming error"),
        AssertionError("synthetic invariant violated"),
    ])
    def test_default_false_unchanged_error_response(self, adapter, exc):
        # 默认 False：与既有消费者完全一致——程序错误仍被吞为 provider=error
        messages: List[Dict[str, Any]] = [{"role": "user", "content": "hi"}]
        with patch.object(
            adapter, "_call_litellm_model", side_effect=exc,
        ):
            resp = adapter.call_text(messages, timeout=10)
        assert resp.provider == "error"
        assert "All LLM models failed" in (resp.content or "")

    def test_flag_never_enters_sdk_kwargs(self, adapter):
        # route flag 只在本地异常边界生效，不进入 SDK kwargs
        observed: Dict[str, Any] = {}

        def _fake_terminal(*args: Any, **kwargs: Any):
            observed.update(kwargs)
            raise TypeError("terminal")

        with patch.object(adapter, "_call_litellm_model", _fake_terminal):
            with pytest.raises(TypeError):
                adapter.call_text([{"role": "user", "content": "hi"}],
                                  timeout=10, propagate_program_errors=True)
        assert "propagate_program_errors" not in observed

    def test_call_with_tools_default_path_unchanged(self, adapter):
        # 既有入口 call_with_tools 不传 opt-in → 默认行为不变
        with patch.object(
            adapter, "_call_litellm_model",
            side_effect=TypeError("synthetic programming error"),
        ):
            resp = adapter.call_with_tools(
                [{"role": "user", "content": "hi"}], tools=[], timeout=10)
        assert resp.provider == "error"

    @staticmethod
    def _sdk_wrapped_typeerror() -> tuple[Any, TypeError]:
        """构造与 root 实录同构的真实 SDK 包装链（LiteLLM 已装类）：

        InternalServerError → __context__ OpenAIError → __context__
        APIConnectionError → __cause__ builtins.TypeError（root-
        standards-exception-chain 逐对象记录的拓扑）。返回 (包装异常,
        底层 TypeError)。
        """
        import litellm
        from litellm.llms.openai.common_utils import OpenAIError
        from openai import APIConnectionError

        terminal = TypeError("httpx.AsyncClient.send terminal programming error")
        conn_err = APIConnectionError(
            request=httpx.Request("POST", "https://llm.example.com/v1"))
        conn_err.__cause__ = terminal
        openai_err = OpenAIError(
            status_code=500, message="OpenAIException - Connection error.")
        openai_err.__context__ = conn_err
        sdk_err = litellm.exceptions.InternalServerError(
            message="litellm.InternalServerError: InternalServerError: "
                    "OpenAIException - Connection error.",
            llm_provider="openai", model="synthetic-standard",
        )
        sdk_err.__context__ = openai_err
        return sdk_err, terminal

    def test_sdk_wrapped_program_error_rethrown_from_chain(self, adapter):
        # root 反例（边界终态）：真实 SDK 包装后外层 isinstance 挡不住——
        # opt-in 遍历真实异常对象链，rethrow **底层原对象**（身份一致）
        sdk_err, terminal = self._sdk_wrapped_typeerror()
        messages: List[Dict[str, Any]] = [{"role": "user", "content": "hi"}]
        with patch.object(adapter, "_call_litellm_model", side_effect=sdk_err):
            with pytest.raises(TypeError) as excinfo:
                adapter.call_text(messages, timeout=10,
                                  propagate_program_errors=True)
        assert excinfo.value is terminal  # rethrow 的是链上的真实底层对象
        assert "All LLM models failed" not in str(excinfo.value)

    def test_default_false_sdk_wrapped_chain_unchanged(self, adapter):
        # 默认 False：同一 SDK 包装链逐字节保持既有行为（provider=error）
        sdk_err, _ = self._sdk_wrapped_typeerror()
        messages: List[Dict[str, Any]] = [{"role": "user", "content": "hi"}]
        with patch.object(adapter, "_call_litellm_model", side_effect=sdk_err):
            resp = adapter.call_text(messages, timeout=10)
        assert resp.provider == "error"
        assert "All LLM models failed" in (resp.content or "")

    def test_remote_error_mentioning_typeerror_not_misinterpreted(self, adapter):
        # 真·远端服务失败：HTTP500 消息**文本**提到 TypeError，但链上无真实
        # TypeError/AssertionError 对象 → opt-in 也不误判为本地程序错误
        # （只查对象，不匹配错误文本/类名子串）
        import litellm

        remote = litellm.exceptions.InternalServerError(
            message='upstream 500: {"error": "TypeError in remote handler"}',
            model="synthetic-standard", llm_provider="openai",
        )
        messages: List[Dict[str, Any]] = [{"role": "user", "content": "hi"}]
        with patch.object(adapter, "_call_litellm_model", side_effect=remote):
            resp = adapter.call_text(messages, timeout=10,
                                     propagate_program_errors=True)
        assert resp.provider == "error"

    def test_cycle_chain_bounded_and_safe(self, adapter):
        # 自引用/互相引用环 + 深链：有界遍历正常返回/抛出，不失控
        a = TypeError("cycle-a")
        b = RuntimeError("cycle-b")
        a.__cause__ = b
        b.__context__ = a  # 环
        sdk_err, _ = self._sdk_wrapped_typeerror()
        # 把环挂到链末端，验证 visited 集合终止
        leaf = RuntimeError("leaf")
        leaf.__cause__ = a
        sdk_err.__context__.__context__.__context__ = leaf
        messages: List[Dict[str, Any]] = [{"role": "user", "content": "hi"}]
        with patch.object(adapter, "_call_litellm_model", side_effect=sdk_err):
            with pytest.raises(TypeError):
                adapter.call_text(messages, timeout=10,
                                  propagate_program_errors=True)


class TestSectorSynthesisRealAdapter:
    """sector 综合走真实 adapter（合成 wire；校验/登记表真实）。"""

    def _synthesis(self, monkeypatch: pytest.MonkeyPatch, content: str):
        from datetime import datetime

        from src.services import sector_analysis_service as svc

        _configure_synthetic(monkeypatch)
        today = datetime.now().date().isoformat()
        cat = {
            "eligible": True, "query": "q", "window_days": 3,
            "window_start": today, "window_end": today,
            "observed_at": "t", "provider": "S",
            "items": [{"evidence_id": "cat-1", "title": "白酒行业新政策发布",
                       "url": "https://example.com/news/1",
                       "source_domain": "example.com", "snippet": "新规落地",
                       "published_date": today, "date_basis": "provider_published",
                       "date_basis_detail": None, "same_event_urls": []}],
            "background_items": [], "dropped_duplicate_url": 0,
            "dropped_syndicated": 0, "dropped_offwindow": 0,
            "dropped_offtopic": 0, "dropped_invalid_source": 0,
            "source": "s", "gaps": [],
        }
        ident = {"industry_em": "白酒Ⅱ", "board_name": "白酒Ⅱ",
                 "board_code": "BK1277", "subject_status": "confirmed",
                 "membership": {"verified": True, "member_count": 20},
                 "sector_name": "白酒Ⅱ", "gaps": []}
        prosper = {"sector_found": True, "momentum": 80.0, "rank_of": 1,
                   "total_boards": 400, "breadth": 60.0,
                   "row": {"change_pct": 1.65}, "gaps": []}
        trend = {"eligible": True, "start_date": "2026-10-01",
                 "end_date": "2026-10-30", "qualified_sessions": 20,
                 "period_return_pct": 5.0,
                 "benchmark": {"period_return_pct": 3.0},
                 "relative_return_pp": 2.0, "gaps": []}
        peers = {"eligible": True,
                 "items": [{"name": "贵州茅台", "is_target": True, "change_pct": 1.5},
                           {"name": "五粮液", "is_target": False, "change_pct": 0.9}],
                 "gaps": []}
        from src.services.sector_analysis_service import ReportBudget

        wire, _ = _install_synthetic_wire(content)
        with wire:
            return svc._synthesis_pillar(
                "贵州茅台", "600519", ident, prosper, trend, peers, cat,
                budget=ReportBudget(30.0), subject_confirmed=True,
            )

    def test_configured_real_adapter_wired_success(self, monkeypatch):
        # 自然定性 complete 控制：无数字的证据关联解读照常通过（status=ok）
        content = json.dumps({"claims": [
            {"kind": "interpretation",
             "text": "白酒行业新政策构成近期催化，目标股在成交活跃同业中涨幅居前",
             "evidence_refs": ["cat-1", "peers"]},
        ]}, ensure_ascii=False)
        out = self._synthesis(monkeypatch, content)
        assert out["status"] == "ok", out["gaps"]
        assert out["claims"]
        assert out["provider"] and out["model"]

    def test_quantitative_claim_rejected_status_malformed(self, monkeypatch):
        # 真实 wire + 真实登记表：模型输出定量陈述（即使数值与证据一致）
        # → 整条拒绝 → malformed_output；数字事实由服务端渲染
        content = json.dumps({"claims": [
            {"kind": "interpretation",
             "text": "板块区间收益5.1234%大幅领先", "evidence_refs": ["cat-1"]},
        ]}, ensure_ascii=False)
        out = self._synthesis(monkeypatch, content)
        assert out["status"] == "malformed_output"
        assert out["claims"] == []

    def test_wrong_owner_numeric_claim_rejected_even_when_value_in_refs(self, monkeypatch):
        # root 反例的定性契约形态：数值 1.5 虽是所引 peers 材料里的真实数，
        # 但陈述重述数值本身就是定量陈述 → 拒绝（不存在跨主体借用通道）
        content = json.dumps({"claims": [
            {"kind": "interpretation",
             "text": "目标股快照涨跌幅1.5%居前", "evidence_refs": ["peers"]},
        ]}, ensure_ascii=False)
        out = self._synthesis(monkeypatch, content)
        assert out["status"] == "malformed_output"
        assert out["claims"] == []
