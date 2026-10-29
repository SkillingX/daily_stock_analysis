# -*- coding: utf-8 -*-
"""六维补充评分契约测试（issue #41）。

已有研究员评分不得被补充评分缺口覆盖；评分事实必须是完整合法 JSON；
单次调用超时继承全局配置并受剩余研究预算限制。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.research_run import ResearchRun, current_research
from src.deep_research_dims import six_dim
from src.deep_research_dims.context import SharedContext


@pytest.fixture(autouse=True)
def _no_history_fetch(monkeypatch: pytest.MonkeyPatch):
    """离线套件不得打真实网络：共享快照不足 1050 根时周线会触发补取（issue #42），
    fixture 股票代码的真实 DB/供应商请求是无关副作用，统一按取数失败降级。"""
    monkeypatch.setattr(
        "src.services.history_loader.load_history_df",
        lambda *args, **kwargs: (None, "none"),
    )


def _ctx() -> SharedContext:
    ctx = SharedContext("fixture", "fixture", "2026-10-23")
    ctx.quote = {"price": 100.0}
    ctx.history = [{"high": 101.0, "low": 99.0, "close": 100.0} for _ in range(60)]
    return ctx


class ErrorAdapter:
    """模拟调用失败。"""

    def call_text(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        raise TimeoutError("simulated timeout")


def _scores(dimensions: list[Any], dim_name: str) -> dict[str, tuple[float, bool]]:
    """维度 → {指标名: (分数, 是否缺口)}。"""
    for d in dimensions:
        if d.dimension == dim_name:
            return {i.name: (i.score, i.data_gap) for i in d.indicators}
    return {}


class TestExistingScoresPreserved:
    """80/76/72 反例：补充调用不可用/失败/空返回时已有合法评分保留。

    issue #64（v2.2）：股权高管（equity_mgmt）退休——治理评分不再进入基本面，
    传入 ownership 载荷也不再改变任何指标。
    """

    def test_adapter_unavailable_keeps_researcher_scores(self):
        ctx = _ctx()
        dim = six_dim.build_six_dim(
            ctx, {}, None,
            sentiment_payload={"status": "ok", "institute_score": 76, "community_score": 72, "narrative": "已有机构与社区研究事实"},
        )
        basic = _scores(dim.framework.dimensions, "基本面")
        assert "股权架构与高管" not in basic, "退休治理指标不得出现在基本面"
        sentiment = _scores(dim.framework.dimensions, "情绪面")
        assert sentiment["机构评价"] == (76.0, False)
        # issue #46：社区评价退休，不再出现在情绪面指标中（也不算缺口）
        assert "社区评价" not in sentiment

    def test_adapter_failure_keeps_researcher_scores(self):
        ctx = _ctx()
        dim = six_dim.build_six_dim(
            ctx, {}, ErrorAdapter(),
            sentiment_payload={"status": "ok", "institute_score": 76, "community_score": 72, "narrative": "研究事实"},
        )
        assert "股权架构与高管" not in _scores(dim.framework.dimensions, "基本面")
        sentiment = _scores(dim.framework.dimensions, "情绪面")
        assert sentiment["机构评价"] == (76.0, False)
        assert "社区评价" not in sentiment
        # 宏观两项保持缺口，不冒称模型真实中性判断
        assert _scores(dim.framework.dimensions, "宏观")["宏观流动性"] == (50.0, True)

    def test_empty_indicators_response_keeps_researcher_scores(self):
        class EmptyAdapter:
            def call_text(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                return SimpleNamespace(content='{"indicators": []}', provider="fixture", finish_reason="stop")

        ctx = _ctx()
        dim = six_dim.build_six_dim(
            ctx, {}, EmptyAdapter(),
            sentiment_payload={"status": "ok", "institute_score": 76, "community_score": 72, "narrative": "研究事实"},
        )
        assert _scores(dim.framework.dimensions, "情绪面")["机构评价"] == (76.0, False)

    def test_partial_indicators_response_fills_only_returned(self):
        class PartialAdapter:
            def call_text(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                body = (
                    '{"indicators": [{"id": "liquidity", "score": 63, '
                    '"basis_sentence": "成交活跃度与换手数据支持该评分判断"}]}'
                )
                return SimpleNamespace(content=body, provider="fixture", finish_reason="stop")

        ctx = _ctx()
        dim = six_dim.build_six_dim(ctx, {}, PartialAdapter())
        macro = _scores(dim.framework.dimensions, "宏观")
        assert macro["宏观流动性"] == (63.0, False)
        assert macro["风险偏好"] == (50.0, True)

    def test_call_success_unreturned_indicator_not_blamed_as_failure(self):
        """调用成功但模型按纪律未返回某指标 → 归因为未形成评分，不是调用失败。"""

        class PartialAdapter:
            def call_text(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                return SimpleNamespace(
                    content='{"indicators": [{"id": "liquidity", "score": 63, "basis_sentence": "成交活跃度支持该评分判断"}]}',
                    provider="fixture", finish_reason="stop",
                )

        results = six_dim._score_llm_indicators(_ctx(), "fixture", PartialAdapter())
        assert "调用失败" not in results["risk_appetite"]["summary"]
        assert "未对该指标形成评分" in results["risk_appetite"]["summary"]

    def test_call_exception_blamed_as_failure(self):
        results = six_dim._score_llm_indicators(_ctx(), "fixture", ErrorAdapter())
        assert "调用失败" in results["risk_appetite"]["summary"]

    def test_no_extra_call_when_all_indicators_covered(self):
        calls: list[Any] = []

        class SpyAdapter:
            def call_text(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                calls.append(kwargs)
                return SimpleNamespace(content="{}", provider="fixture", finish_reason="stop")

        ctx = _ctx()
        existing = {ind_id: {"id": ind_id, "score": 60.0, "summary": "已有", "data_gap": False} for ind_id in six_dim._LLM_INDICATOR_IDS}
        results = six_dim._score_llm_indicators(ctx, "fixture", SpyAdapter(), None, existing=existing)
        assert results == {}
        assert calls == []


class TestScoringFactsJson:
    """给模型的事实必须是完整合法 JSON，不靠截断序列化字符串。"""

    def _captured_user_content(self) -> tuple[type, list[list[dict[str, Any]]]]:
        captured: list[list[dict[str, Any]]] = []

        class CaptureAdapter:
            def call_text(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                captured.append(messages)
                return SimpleNamespace(content='{"indicators": []}', provider="fixture", finish_reason="stop")

        return CaptureAdapter, captured

    def test_large_facts_remain_valid_json(self):
        ctx = _ctx()
        ctx.fundamental = {"large_evidence": "证据" * 2000}
        adapter_cls, captured = self._captured_user_content()
        six_dim._score_llm_indicators(ctx, "fixture", adapter_cls())
        user = captured[-1][-1]["content"]
        raw = user.split("```json\n", 1)[1].split("\n```", 1)[0]
        parsed = json.loads(raw)  # 截断序列化会在这里抛 ValueError
        assert isinstance(parsed, dict)

    def test_large_dict_keeps_subfields_instead_of_whole_key(self):
        """fundamental 过大时按子键保留核心字段与宏观证据，不再整块删除。"""
        ctx = _ctx()
        ctx.fundamental = {
            "pe_ttm": 12.5,
            "macro_note": "央行公告：维持流动性合理充裕",
            "large_evidence": "证据" * 2000,
        }
        adapter_cls, captured = self._captured_user_content()
        six_dim._score_llm_indicators(ctx, "fixture", adapter_cls())
        raw = captured[-1][-1]["content"].split("```json\n", 1)[1].split("\n```", 1)[0]
        parsed = json.loads(raw)
        assert parsed["fundamental"]["macro_note"] == "央行公告：维持流动性合理充裕"
        assert "large_evidence" not in parsed["fundamental"]
        user = captured[-1][-1]["content"]
        assert "fundamental.large_evidence" in user.split("```", 2)[2]

    def test_nested_research_keeps_short_evidence_for_missing_indicator(self):
        """审查 P2：research 含大 financial_series 时，简短公司资料证据仍保留给补评。"""
        ctx = _ctx()
        ctx.fundamental = {
            "pe_ttm": 12.5,
            "research": {
                "financial_series": "营收与净利十年序列" * 400,
                "controller": "实控人：张某（公告 2026-08-30 披露）",
                "holder_notice": "股东增持公告来源：巨潮资讯 2026-09-12",
            },
        }
        adapter_cls, captured = self._captured_user_content()
        # 只缺 risk_appetite → research 按优先键最先进裁剪
        results = six_dim._score_llm_indicators(
            ctx, "fixture", adapter_cls(), None,
            existing={k: {"score": 60.0} for k in ("institute_view", "liquidity")},
        )
        raw = captured[-1][-1]["content"].split("```json\n", 1)[1].split("\n```", 1)[0]
        parsed = json.loads(raw)
        research = parsed.get("fundamental", {}).get("research", {})
        assert research.get("controller") == "实控人：张某（公告 2026-08-30 披露）"
        assert "holder_notice" in research
        assert "financial_series" not in research
        assert "fundamental.research.financial_series" in captured[-1][-1]["content"].split("```", 2)[2]

    def test_omitted_fields_disclosed(self):
        ctx = _ctx()
        ctx.fundamental = {"large_evidence": "证据" * 2000}
        adapter_cls, captured = self._captured_user_content()
        six_dim._score_llm_indicators(ctx, "fixture", adapter_cls())
        user = captured[-1][-1]["content"]
        assert "省略" in user

    def test_normal_facts_kept_whole(self):
        ctx = _ctx()
        ctx.fundamental = {"pe_ttm": 12.5, "roe": 8.9}
        ctx.limitations = ["日线部分缺失"]
        adapter_cls, captured = self._captured_user_content()
        six_dim._score_llm_indicators(ctx, "fixture", adapter_cls())
        raw = captured[-1][-1]["content"].split("```json\n", 1)[1].split("\n```", 1)[0]
        parsed = json.loads(raw)
        assert parsed["fundamental"] == {"pe_ttm": 12.5, "roe": 8.9}
        assert parsed["limitations"] == ["日线部分缺失"]


class TestScoringTimeout:
    def _spy(self) -> tuple[Any, list[dict[str, Any]]]:
        captured: list[dict[str, Any]] = []

        class Spy:
            def call_text(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                captured.append(kwargs)
                return SimpleNamespace(content='{"indicators": []}', provider="fixture", finish_reason="stop")

        return Spy(), captured

    def test_timeout_inherits_configured_cap(self, monkeypatch: pytest.MonkeyPatch):
        from src.config import get_config
        monkeypatch.setattr(get_config(), "llm_stream_response_timeout", 300.0)
        spy, captured = self._spy()
        six_dim._score_llm_indicators(_ctx(), "fixture", spy)
        assert captured and 0 < captured[-1]["timeout"] <= 300.0

    def test_timeout_limited_by_remaining_budget(self, monkeypatch: pytest.MonkeyPatch):
        from src.config import get_config
        monkeypatch.setattr(get_config(), "llm_stream_response_timeout", 300.0)
        run = ResearchRun(600.0)
        run.deadline -= 550.0  # 剩余 50 秒
        spy, captured = self._spy()
        token = current_research.set(run)
        try:
            six_dim._score_llm_indicators(_ctx(), "fixture", spy)
        finally:
            current_research.reset(token)
        assert captured and captured[-1]["timeout"] <= 50.0

    def test_exhausted_budget_skips_request(self, monkeypatch: pytest.MonkeyPatch):
        from src.config import get_config
        monkeypatch.setattr(get_config(), "llm_stream_response_timeout", 300.0)
        run = ResearchRun(600.0)
        run.deadline -= 700.0  # 预算耗尽
        spy, captured = self._spy()
        token = current_research.set(run)
        try:
            results = six_dim._score_llm_indicators(_ctx(), "fixture", spy)
        finally:
            current_research.reset(token)
        assert captured == []
        assert results and all(r["data_gap"] for r in results.values())

    def test_hardcoded_90s_constant_removed(self):
        import inspect
        source = inspect.getsource(six_dim._score_llm_indicators)
        assert "90.0" not in source


class TestGapReasons:
    def test_reasons_distinguish_failure_modes(self):
        reasons: dict[str, str] = {}

        class ReasonAdapter:
            def call_text(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                return SimpleNamespace(content="not json", provider="fixture", finish_reason="stop")

        ctx = _ctx()
        results = six_dim._score_llm_indicators(ctx, "fixture", ReasonAdapter())
        reasons["invalid_json"] = results["liquidity"]["summary"]
        results_none = six_dim._score_llm_indicators(ctx, "fixture", None)
        reasons["no_model"] = results_none["liquidity"]["summary"]
        assert "响应" in reasons["invalid_json"]
        assert "模型不可用" in reasons["no_model"]
        # 没有证据不得推断欠费/额度
        assert "额度" not in reasons["invalid_json"] and "欠费" not in reasons["invalid_json"]
