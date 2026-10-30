# -*- coding: utf-8 -*-
"""Issue #68：情报行情依据统一 + 事件预案收敛为研究核验动作。

四条契约在真实入口验证（离线，fake LLM / 零网络 / 零真实供应商）：
1. 消息构建边界：真实 ``run_intel_agent`` 出站消息携带本报告已采用行情及
   来源/时点/资格与文献引用边界；变价即变消息；无行情如实披露；不再要求
   独立交易预案或 T1 编号。
2. 采用边界：真实 ``_parse_intel`` 拒绝独立交易指令（含 T1 编号/仓位/止损）
   并随 truncation_notes 披露，核验动作与研报引用保留（细节见
   ``test_parse_intel.py``；此处只验证与消息/缓存契约的协同）。
3. 经理波 C 与波 A/B 使用同一 ``_market_digest`` 市场依据（含行情 provenance，
   缺证据时披露，不用装配时钟冒充观测时点）。
4. 缓存：intel 契约版本 + 已采用行情身份（同身份 hit；变价/跨观测时点/
   无行情/身份不足 miss；旧契约重算），非 intel 维度行为不变；垂直链上
   走真实 ``try_cache``/``store_cache``。

边界：本票不改写历史报告、不承诺消灭模型任意事实错误；离线注入验证
不冒充供应商原始录制或真实报告语义验收。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from src.agent.llm_adapter import LLMResponse
from src.core.research_run import ResearchRun
from src.deep_research_dims import dim_cache
from src.deep_research_dims.context import SharedContext
from src.deep_research_dims.dim_cache import quote_market_identity
from src.deep_research_dims.manager_writeup import generate_manager_writeup
from src.schemas.deep_research_dims import ACTIVE_DIM_IDS, DIM_MODELS

from tests.test_choice_integration import choice_transport  # noqa: F401
from tests.test_issue64_no_target_mode import (
    _DeterministicResearchAdapter,
    _NO_TARGET_RESEARCH_DIMS,
    _assert_single_done_terminal,
    _no_target_vertical_env,
    _sse_events,
)


# ---------------------------------------------------------------------------
# 共享 fixture 形态
# ---------------------------------------------------------------------------

def _quote_with_meta(price: float, *, observed_at: str = "2026-10-30 15:00:03", source: str = "eastmoney") -> Dict[str, Any]:
    return {
        "price": price,
        "field_meta": {
            "current_price": {
                "source": source,
                "observed_at": observed_at,
                "rule_eligible": True,
                "input_reasons": [],
                "unit": "currency_base",
            }
        },
    }


_INTEL_COMPLETION = {
    "sentiment_summary": "公告与行业新闻中性偏正面",
    "earnings_outlook": "订单回暖",
    "risk_alerts": ["原料价格波动"],
    "positive_catalysts": ["下游补库"],
    "latest_news": "披露订单回暖公告",
    "unverified_count": 0,
    "evidence_items": [],
    "root_cause": {},
    "event_calendar": [],
}


class _IntelMessageCapture:
    """真实 run_intel_agent 出站消息捕获；无工具调用，单步固定完成。"""

    def __init__(self) -> None:
        self.system_messages: List[str] = []
        self.user_messages: List[str] = []

    def call_with_tools(
        self, messages: List[Dict[str, Any]], tool_decls: List[Any], timeout: Any = None,
    ) -> LLMResponse:
        self.system_messages.append(
            str(next(m.get("content") or "" for m in messages if m.get("role") == "system"))
        )
        self.user_messages.append(
            str(next(m.get("content") or "" for m in messages if m.get("role") == "user"))
        )
        return LLMResponse(
            content=json.dumps(_INTEL_COMPLETION, ensure_ascii=False),
            provider="fixture", model="fixture", finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )


def _run_intel_once(quote: Dict[str, Any]) -> tuple[str, str]:
    """在绑定共享行情的 ResearchRun 内真实调用 run_intel_agent，返回 (system, user) 消息。"""
    from src.agent.deep_research.explore_agents import run_intel_agent

    run = ResearchRun(600.0)
    run.context = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00", quote=quote)
    capture = _IntelMessageCapture()
    with run.bind():
        out = run_intel_agent("300398", "飞凯材料", capture)
    assert out.get("ok"), out
    assert len(capture.user_messages) == 1
    return capture.system_messages[0], capture.user_messages[0]


class TestIntelMessageCarriesAdoptedQuote:
    """契约 1：真实消息构建边界的行情依据与职责覆盖。"""

    def test_quote_change_changes_message_and_carries_provenance(self) -> None:
        _, low = _run_intel_once(_quote_with_meta(26.34))
        _, high = _run_intel_once(_quote_with_meta(77.77))
        # 变价即变消息：两组分别携带各自已采用行情，互不包含
        assert "现价 26.34" in low and "26.34" not in high
        assert "现价 77.77" in high and "77.77" not in low
        for message in (low, high):
            assert "行情时点 2026-10-30 15:00:03" in message, "行情观测时点随消息注入"
            assert "来源 eastmoney" in message, "行情来源随消息注入"
            assert "文献摘录" in message, "搜索摘录报价的文献边界注入"
        # 同价跨真实观测时点是不同身份：消息携带时点后可区分
        _, later = _run_intel_once(_quote_with_meta(26.34, observed_at="2026-10-31 09:31:00"))
        assert "2026-10-31 09:31:00" in later

    def test_missing_quote_disclosed_not_fabricated(self) -> None:
        message = _run_intel_once({})[1]
        assert "未取得有效实时行情" in message
        assert "不得据此推算相对目标空间" in message
        assert "日期未知" in message

    def test_quote_without_identity_fields_stays_honest(self) -> None:
        message = _run_intel_once({"price": 26.34})[1]
        assert "现价 26.34" in message
        assert "未记录" in message, "缺来源/时点时如实标注，不伪造"

    def test_conflict_qualification_carried_not_rebranded(self) -> None:
        """coord 反例：quality=conflict + rule_eligible=False 原样传递，不粉饰成普通单源。"""
        quote = _quote_with_meta(26.34)
        quote["field_meta"]["current_price"].update({
            "rule_eligible": False,
            "quality": {"status": "conflict", "reason_codes": []},
        })
        message = _run_intel_once(quote)[1]
        assert "资格 不合格" in message
        assert "质量状态 conflict" in message
        assert "合格（单源数据，未独立核验）" not in message

    def test_verified_quality_not_labelled_unverified_single_source(self) -> None:
        """coord 收口：rule_eligible=True 只投影「合格」，quality（verified）单独
        携带；不得从 True 推断「单源/未独立核验」。"""
        quote = _quote_with_meta(26.34)
        quote["field_meta"]["current_price"]["quality"] = {"status": "verified", "reason_codes": []}
        message = _run_intel_once(quote)[1]
        assert "资格 合格" in message
        assert "质量状态 verified" in message
        assert "未独立核验" not in message and "单源数据" not in message

    def test_manager_eligibility_text_does_not_infer_single_source(self) -> None:
        """经理 provenance 同口径：True→合格，quality_status 独立字段携带。"""
        capture = _ManagerCapture()
        run = ResearchRun(600.0)
        quote = _quote_with_meta(26.34)
        quote["field_meta"]["current_price"]["quality"] = {"status": "verified", "reason_codes": []}
        run.context = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00", quote=quote)
        with run.bind():
            generate_manager_writeup(capture, "飞凯材料", "300398", _manager_dims(), [])
        provenance = json.loads(capture.sent[0])["completed_research"]["market"]["quote_provenance"]
        assert provenance["eligibility"] == "合格"
        assert provenance["quality_status"] == "verified"
        assert "未独立核验" not in provenance["eligibility"]

    def test_unknown_eligibility_stays_unknown(self) -> None:
        """rule_eligible 缺失时按未知，不默认合格。"""
        quote = {"price": 26.34, "field_meta": {"current_price": {
            "source": "eastmoney", "observed_at": "2026-10-30 15:00:03",
        }}}
        message = _run_intel_once(quote)[1]
        assert "资格 未知" in message


class _FakeProviderQuote:
    """duck-typed UnifiedRealtimeQuote：无效标量反例走真实 _safe_quote 采纳缝。"""

    def __init__(self, price: Any) -> None:
        self.price = price
        self.change_percent = None
        self.change_pct = None
        self.volume = 12345
        self.turnover = 1.5
        self.name = "飞凯材料"
        self.source = "eastmoney"
        self.currency = "CNY"
        self.provider_timestamp = "2026-10-30 15:00:03"
        self.fetched_at = "2026-10-30 15:00:04"
        self.is_stale = False
        self.field_meta: Dict[str, Any] = {}


class TestInvalidQuoteNotAuthoritative:
    """spec 反例：0/负/NaN/Inf/bool 等无效标量不得作为权威现价注入任何消息，
    也不得经采纳缝流入 facts/保存 JSON；有效标量照常采用。"""

    @pytest.mark.parametrize("bad_price", [0, -5.2, float("inf"), float("nan"), True, "26.34"])
    def test_invalid_price_takes_no_current_data_branch(self, bad_price: Any) -> None:
        message = _run_intel_once({"price": bad_price})[1]
        assert f"现价 {bad_price}" not in message
        assert "未取得有效实时行情" in message
        assert "不得据此推算相对目标空间" in message
        assert "禁止把搜索摘录或新闻中的任何报价当作本次现价" in message

    @pytest.mark.parametrize("bad_price", [float("inf"), 0, -1.0])
    def test_stage_zero_adoption_drops_invalid_price_scalar(
        self, monkeypatch: pytest.MonkeyPatch, bad_price: Any
    ) -> None:
        """阶段 0 采纳缝：无效标量不入共享上下文，其余有效字段保留并披露。"""
        import src.agent.tools.data_tools as data_tools
        from src.deep_research_dims.context import SharedContext, _safe_quote

        manager = type("Manager", (), {})()
        manager.get_realtime_quote = lambda code: _FakeProviderQuote(bad_price)  # type: ignore[method-assign]
        monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: manager)
        ctx = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00")
        payload = _safe_quote("300398", ctx)
        assert payload["price"] is None, "无效标量不得成为已采用现价"
        assert payload["volume"] == 12345, "其余有效字段保留"
        assert any("现价无效" in note for note in ctx.limitations)
        assert quote_market_identity(payload) == {"absent": True}

    def test_stage_zero_adoption_keeps_valid_price(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.agent.tools.data_tools as data_tools
        from src.deep_research_dims.context import SharedContext, _safe_quote

        manager = type("Manager", (), {})()
        manager.get_realtime_quote = lambda code: _FakeProviderQuote(26.34)  # type: ignore[method-assign]
        monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: manager)
        ctx = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00")
        payload = _safe_quote("300398", ctx)
        assert payload["price"] == 26.34
        assert not ctx.limitations

    def test_investment_duties_converged_in_system_message(self) -> None:
        """宪法投资决策职责在 system 层收敛（宪法之后、dim prompt 之后）。"""
        system, user = _run_intel_once(_quote_with_meta(26.34))
        assert "本轮职责边界（分区情报研究员" in system, "职责边界注入 system 消息"
        assert system.index("共享宪法") < system.index("本轮职责边界"), "边界置于宪法之后"
        assert "不在本轮输出义务内" in system, "宪法投资决策职责被显式覆盖"
        assert "PM 七问" in system, "宪法第 5 条问项收敛"
        assert "研究核验/跟踪动作" in system
        assert "规则作战计划" in system
        assert "仍然有效的纪律" in system, "来源/缺口/禁止编造纪律保留"
        # user 任务不再要求交易预案；旧任务要求措辞不出现
        assert "事件预案编号联动" not in user
        assert "左侧买入" not in user and "右侧确认买入" not in user
        assert "每个结果对应的一条交易预案" not in user
        assert "研究核验" in user
        assert "不夹带交易指令" in user
        assert "保留原文并带来源等级" in user, "研报引用合法保留"


# ---------------------------------------------------------------------------
# 契约 3：经理波 C 与 A/B 同一市场依据（含 provenance）
# ---------------------------------------------------------------------------

def _manager_dims() -> dict:
    from src.schemas.report_schema import DataPerspective, PricePosition

    dims = {d: DIM_MODELS[d]() for d in ACTIVE_DIM_IDS}
    dims["data"] = dims["data"].model_copy(update={
        "narrative": "现价 26.34 元附近震荡，量能温和。",
        "perspective": DataPerspective(price_position=PricePosition(current_price=26.34)),
    })
    return dims


class _ManagerCapture:
    """按经理波次返回最小完成 JSON，并捕获全部出站 system/user 消息。"""

    def __init__(self) -> None:
        self.sent: List[str] = []
        self.system_messages: List[str] = []

    def call_text(self, messages: List[Dict[str, Any]], **kwargs: Any) -> LLMResponse:
        system = str(messages[0].get("content") or "")
        self.system_messages.append(system)
        self.sent.append(str(messages[1].get("content") or ""))
        if '"title_angle"' in system:
            payload: Dict[str, Any] = {"title_angle": "观察", "executive_summary": "定性判断。" * 20}
        elif '"thesis_points"' in system:
            payload = {"section_narratives": {}}
        else:
            payload = {"section_narratives": {}}
        return LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            provider="fixture", model="fixture", finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )


class TestManagerWaveCMarketDigest:
    def test_wave_c_shares_market_digest_with_a_and_b(self) -> None:
        """波 C 与波 A/B 使用同一 _market_digest：现价与 provenance 同源。"""
        capture = _ManagerCapture()
        run = ResearchRun(600.0)
        run.context = SharedContext(
            "300398", "飞凯材料", "2026-10-30T15:00:00", quote=_quote_with_meta(26.34)
        )
        with run.bind():
            generate_manager_writeup(capture, "飞凯材料", "300398", _manager_dims(), [])
        assert len(capture.sent) == 3
        wave_a, wave_b, wave_c = (json.loads(message) for message in capture.sent)
        market_a = wave_a["completed_research"]["market"]
        market_c = wave_c["completed_research"]["market"]
        assert market_a["current_price"] == 26.34
        assert market_c["current_price"] == market_a["current_price"]
        assert market_c["quote_provenance"]["source"] == "eastmoney"
        assert market_c["quote_provenance"]["observed_at"] == "2026-10-30 15:00:03"
        assert market_a["quote_provenance"] == market_c["quote_provenance"]
        # 波 B 摘要同样携带（既有行为，回归保护）
        assert wave_b["completed_research"]["market"] == market_a

    def test_market_digest_discloses_missing_provenance(self) -> None:
        """无 run（无共享上下文）时如实披露，不用装配时钟冒充观测时点。"""
        from src.deep_research_dims.manager_writeup import _market_digest

        digest = _market_digest(_manager_dims())
        assert "quote_provenance" not in digest
        assert "未取得有效实时行情" in digest["quote_note"]

    @pytest.mark.parametrize("bad_price", [float("inf"), float("nan"), 0, -3.0])
    def test_market_digest_discloses_invalid_price_not_provenance(self, bad_price: Any) -> None:
        """spec 反例：无效标量在经理波同样走无行情披露，不产出依据记录。"""
        from src.deep_research_dims.manager_writeup import _market_digest

        run = ResearchRun(600.0)
        run.context = SharedContext(
            "300398", "飞凯材料", "2026-10-30T15:00:00",
            quote=_quote_with_meta(bad_price),
        )
        with run.bind():
            digest = _market_digest(_manager_dims())
        assert "quote_provenance" not in digest, "无效标量不得携带已采用行情依据"
        assert "未取得有效实时行情" in digest["quote_note"]

    def test_manager_provenance_preserves_unknown_eligibility(self) -> None:
        """standards 观察：rule_eligible 缺失保留未知，不冒充 False。"""
        capture = _ManagerCapture()
        run = ResearchRun(600.0)
        quote = {"price": 26.34, "field_meta": {"current_price": {
            "source": "eastmoney", "observed_at": "2026-10-30 15:00:03",
        }}}
        run.context = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00", quote=quote)
        with run.bind():
            generate_manager_writeup(capture, "飞凯材料", "300398", _manager_dims(), [])
        wave_a = json.loads(capture.sent[0])
        provenance = wave_a["completed_research"]["market"]["quote_provenance"]
        assert provenance["rule_eligible"] is None
        assert provenance["eligibility"] == "未知"


# ---------------------------------------------------------------------------
# spec-review P2 回归：经理市场摘要现价与已采用行情同源（真实 build_data_dim 链）
# ---------------------------------------------------------------------------

def _history_rows(count: int) -> List[Dict[str, Any]]:
    """构造 OHLCV 历史行；date 只作标识，build_data_dim 不读日期。"""
    return [
        {
            "date": f"2026-fixture-{i:04d}",
            "open": 24.0,
            "high": 25.0 + i * 0.05,
            "low": 23.0,
            "close": round(24.0 + i * 0.1, 2),
            "volume": 1000,
        }
        for i in range(count)
    ]


class TestManagerDigestSameSourceAsSharedQuote:
    """spec-review P2 反例回归：经理摘要现价不得与已采用行情分源。

    两条真实可达反例经真实 ``build_data_dim`` → ``generate_manager_writeup``：
    Repro1 报价有效但历史不足 20 条（data degraded 无 perspective）——三波与
    情报同看到已采用现价；Repro2 缺报价但历史充足（DataDim 回退末日收盘）——
    三波现价如实为 null，历史回退价不冒充已采用现价，与情报披露同语义。
    """

    @staticmethod
    def _dims_with_real_data_dim(quote: Dict[str, Any], history: List[Dict[str, Any]]) -> dict:
        from src.deep_research_dims.data_dim import build_data_dim

        ctx = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00", quote=quote, history=history)
        dims = {d: DIM_MODELS[d]() for d in ACTIVE_DIM_IDS}
        dims["data"] = build_data_dim(ctx)
        return dims

    def test_valid_quote_short_history_keeps_adopted_price_in_all_waves(self) -> None:
        """Repro1：报价 26.34 有效但历史 <20 条 → data degraded 无 perspective。"""
        quote = _quote_with_meta(26.34)
        dims = self._dims_with_real_data_dim(quote, _history_rows(10))
        assert dims["data"].status == "degraded" and dims["data"].perspective is None, (
            "真实链前提：历史不足 20 条时 DataDim 降级且无透视"
        )

        capture = _ManagerCapture()
        run = ResearchRun(600.0)
        run.context = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00", quote=quote)
        with run.bind():
            generate_manager_writeup(capture, "飞凯材料", "300398", dims, [])
        assert len(capture.sent) == 3
        for message in capture.sent:
            market = json.loads(message)["completed_research"]["market"]
            assert market["current_price"] == 26.34, "数据降级不丢失已采用现价"
            assert market["status"] == "degraded", "真实降级状态保留"
            assert market["quote_provenance"]["source"] == "eastmoney"
            assert "quote_note" not in market
        # 情报同语义：同一共享行情作为权威现价注入消息
        assert "现价 26.34" in _run_intel_once(quote)[1]

    def test_missing_quote_history_fallback_not_relabelled_adopted(self) -> None:
        """Repro2：缺报价但历史充足 → DataDim 末日收盘价不冒充已采用现价。"""
        dims = self._dims_with_real_data_dim({}, _history_rows(260))
        data = dims["data"]
        assert data.status == "ok"
        assert data.perspective.price_position.current_price not in (None, 26.34), (
            "真实链前提：缺报价时 DataDim 回退末日收盘价"
        )

        capture = _ManagerCapture()
        run = ResearchRun(600.0)
        run.context = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00", quote={})
        with run.bind():
            generate_manager_writeup(capture, "飞凯材料", "300398", dims, [])
        assert len(capture.sent) == 3
        for message in capture.sent:
            market = json.loads(message)["completed_research"]["market"]
            assert market["current_price"] is None, "历史回退收盘价不得冒充已采用现价"
            assert "quote_provenance" not in market
            assert "未取得有效实时行情" in market["quote_note"]
            assert "narrative" not in market, "以「现价」锚定的叙述不随缺行情摘要携带"
            # 以历史回退价计算的偏离指标不冒充已采用现价口径（DataDim 自身计算不变）
            assert market["distance_from_52w_high_pct"] is None
            assert market["ma250_deviation_pct"] is None
        # 情报同语义：无有效行情走披露分支，不注入权威现价
        assert "未取得有效实时行情" in _run_intel_once({})[1]

    def test_stale_cached_data_dim_defers_to_shared_quote(self) -> None:
        """缓存旧 DataDim 与本次共享行情不一致：现价以共享行情为准，
        旧现价锚的叙述与派生偏离指标不随摘要携带（不把缓存旧价重新标记为
        已采用现价口径）。"""
        stale_dims = self._dims_with_real_data_dim({}, _history_rows(260))
        assert stale_dims["data"].perspective.price_position.current_price != 26.34

        from src.deep_research_dims.manager_writeup import _market_digest

        run = ResearchRun(600.0)
        run.context = SharedContext(
            "300398", "飞凯材料", "2026-10-30T15:00:00", quote=_quote_with_meta(26.34)
        )
        with run.bind():
            digest = _market_digest(stale_dims)
        assert digest["current_price"] == 26.34
        assert digest["quote_provenance"]["source"] == "eastmoney"
        assert "narrative" not in digest
        assert digest["distance_from_52w_high_pct"] is None
        assert digest["ma250_deviation_pct"] is None

    def test_matching_history_narrative_still_carried(self) -> None:
        """既有 live 路径保持：行情与 DataDim 现价锚一致时叙述与派生指标照常携带。"""
        quote = _quote_with_meta(26.34)
        dims = self._dims_with_real_data_dim(quote, _history_rows(260))
        data = dims["data"]
        assert data.status == "ok"
        assert data.perspective.price_position.current_price == 26.34
        assert data.narrative, "真实链前提：叙述含现价锚"

        from src.deep_research_dims.manager_writeup import _market_digest

        run = ResearchRun(600.0)
        run.context = SharedContext("300398", "飞凯材料", "2026-10-30T15:00:00", quote=quote)
        with run.bind():
            digest = _market_digest(dims)
        assert digest["current_price"] == 26.34
        assert digest["narrative"] == data.narrative
        assert digest["distance_from_52w_high_pct"] == data.distance_from_52w_high_pct
        assert digest["ma250_deviation_pct"] == data.ma250_deviation_pct

    def test_data_dim_absent_keeps_adopted_quote_disclosed(self) -> None:
        """data 维度缺席时摘要仍携带已采用现价并如实披露维度缺失，
        有效共享行情不因维度缺失被丢弃。"""
        dims = {d: DIM_MODELS[d]() for d in ACTIVE_DIM_IDS}
        dims.pop("data")

        from src.deep_research_dims.manager_writeup import _market_digest

        run = ResearchRun(600.0)
        run.context = SharedContext(
            "300398", "飞凯材料", "2026-10-30T15:00:00", quote=_quote_with_meta(26.34)
        )
        with run.bind():
            digest = _market_digest(dims)
        assert digest["status"] is None
        assert digest["current_price"] == 26.34
        assert "数据维度缺失" in digest["data_note"]
        assert digest["quote_provenance"]["source"] == "eastmoney"
        assert digest["distance_from_52w_high_pct"] is None
        assert digest["ma250_deviation_pct"] is None
        assert "narrative" not in digest

    def test_market_authority_instruction_shared_by_all_waves(self) -> None:
        """现价权威口径在三波共有的 base_system 一处声明（波内不重复）：
        dims 携带的历史/缓存统计不得覆盖 completed_research.market 的现价或缺失披露。"""
        capture = _ManagerCapture()
        run = ResearchRun(600.0)
        run.context = SharedContext(
            "300398", "飞凯材料", "2026-10-30T15:00:00", quote=_quote_with_meta(26.34)
        )
        with run.bind():
            generate_manager_writeup(capture, "飞凯材料", "300398", _manager_dims(), [])
        assert len(capture.system_messages) == 3
        for system in capture.system_messages:
            assert system.count("唯一已采用现价") == 1, "权威口径指令只在共有 base_system 一处"
            assert "历史/缓存统计" in system and "覆盖或补造本次现价" in system
            assert "其中 market 带本报告已采用行情及依据" not in system, "波 A 旧重复句已上移"
            assert "与其他波次同源" not in system, "波 C 旧重复句已上移"


# ---------------------------------------------------------------------------
# 契约 4：缓存身份矩阵（真实 load/save + 身份构造）
# ---------------------------------------------------------------------------

def _intel_payload() -> Dict[str, Any]:
    return DIM_MODELS["intel"]().model_dump()


class TestQuoteMarketIdentity:
    """身份解码复用 reading_from_field_record（root 探针反例对齐）。"""

    def test_identity_three_states(self) -> None:
        identity = quote_market_identity(_quote_with_meta(26.34))
        assert identity is not None
        assert identity["price"] == 26.34
        assert identity["source"] == "eastmoney"
        assert identity["observed_at"] == "2026-10-30 15:00:03"
        assert identity["rule_eligible"] is True
        assert identity["input_reasons"] == []
        assert identity["quality_status"] == ""
        assert quote_market_identity({}) == {"absent": True}
        assert quote_market_identity({"price": 26.34}) is None, "有价但缺来源/时点：身份不足"
        assert quote_market_identity({"price": 0}) == {"absent": True}
        later = quote_market_identity(_quote_with_meta(26.34, observed_at="2026-10-31 09:31:00"))
        assert later is not None and later["observed_at"] != "2026-10-30 15:00:03"

    def test_scalar_cannot_borrow_mismatched_provenance(self) -> None:
        """scalar 26.34 借用 meta.value=34.83 的旧来源/时点 → 身份不足。"""
        quote = _quote_with_meta(26.34)
        quote["field_meta"]["current_price"]["value"] = 34.83
        assert quote_market_identity(quote) is None

    def test_non_finite_price_cannot_be_identity(self) -> None:
        for bad_price in (float("inf"), float("nan")):
            quote = _quote_with_meta(26.34)
            quote["price"] = bad_price
            quote["field_meta"]["current_price"]["value"] = bad_price
            assert quote_market_identity(quote) == {"absent": True}, (
                f"无效标量 {bad_price} 不是身份不足而是明确无已采用行情"
            )

    def test_invalid_scalar_prices_are_absent_not_authoritative(self) -> None:
        """0/负/bool/非数值与 NaN/Inf 同界：无效标量不构成已采用现价。"""
        for bad_price in (0, -5.2, True, "26.34", float("inf"), float("nan")):
            assert quote_market_identity({"price": bad_price}) == {"absent": True}, bad_price

    def test_identity_preserves_unknown_eligibility(self) -> None:
        """standards 观察：资格三态保留，未知不冒充 False，且与 True 不同身份。"""
        unknown = quote_market_identity({"price": 26.34, "field_meta": {"current_price": {
            "source": "eastmoney", "observed_at": "2026-10-30 15:00:03",
        }}})
        assert unknown is not None and unknown["rule_eligible"] is None
        assert unknown != quote_market_identity(_quote_with_meta(26.34))

    def test_currency_is_part_of_identity(self) -> None:
        base = _quote_with_meta(26.34)
        other = _quote_with_meta(26.34)
        other["field_meta"]["current_price"]["currency"] = "USD"
        left = quote_market_identity(base)
        right = quote_market_identity(other)
        assert left is not None and right is not None
        assert left != right, "币种不同即不同身份，不得跨缓存复用"

    def test_fetch_clock_not_in_identity(self) -> None:
        """同一观测（同价/同时点/同来源）不同 fetched_at 仍是同一身份。"""
        base = _quote_with_meta(26.34)
        refetched = _quote_with_meta(26.34)
        refetched["field_meta"]["current_price"]["fetched_at"] = "2026-10-30T23:59:59+08:00"
        left = quote_market_identity(base)
        right = quote_market_identity(refetched)
        assert left is not None and left == right


class TestIntelCacheIdentityMatrix:
    def _prepare(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))

    def test_same_identity_hits_and_changed_price_misses(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prepare(tmp_path, monkeypatch)
        identity_a = quote_market_identity(_quote_with_meta(26.34))
        dim_cache.save_cached_dim("300398", "intel", _intel_payload(), quote_identity=identity_a)
        loaded = dim_cache.load_cached_dim("300398", "intel", quote_identity=identity_a)
        assert loaded is not None
        # 变价 / 同价跨观测时点 / 无行情 / 身份不足 → miss
        assert dim_cache.load_cached_dim(
            "300398", "intel", quote_identity=quote_market_identity(_quote_with_meta(77.77))
        ) is None
        assert dim_cache.load_cached_dim(
            "300398", "intel",
            quote_identity=quote_market_identity(_quote_with_meta(26.34, observed_at="2026-10-31 09:31:00")),
        ) is None
        assert dim_cache.load_cached_dim("300398", "intel", quote_identity={"absent": True}) is None
        assert dim_cache.load_cached_dim("300398", "intel", quote_identity=None) is None

    def test_absent_quote_cache_reused_only_for_absent(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self._prepare(tmp_path, monkeypatch)
        dim_cache.save_cached_dim(
            "300398", "intel", _intel_payload(), quote_identity={"absent": True}
        )
        assert dim_cache.load_cached_dim("300398", "intel", quote_identity={"absent": True}) is not None
        assert dim_cache.load_cached_dim(
            "300398", "intel", quote_identity=quote_market_identity(_quote_with_meta(26.34))
        ) is None

    def test_insufficient_identity_not_saved(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """身份不足的情报不落盘：下次无法核验复用身份，宁重算不伪造。"""
        self._prepare(tmp_path, monkeypatch)
        dim_cache.save_cached_dim("300398", "intel", _intel_payload(), quote_identity=None)
        assert dim_cache.load_cached_dim("300398", "intel", quote_identity=None) is None
        assert dim_cache.load_cached_dim("300398", "intel", quote_identity={"absent": True}) is None

    def test_missing_record_or_caller_identity_is_miss(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """记录缺 quote_identity 字段或调用方身份不足（None）都不得视为相等。"""
        import os
        from pathlib import Path

        self._prepare(tmp_path, monkeypatch)
        os.makedirs(dim_cache._CACHE_DIR, exist_ok=True)
        identity = quote_market_identity(_quote_with_meta(26.34))
        dim_cache.save_cached_dim("300398", "intel", _intel_payload(), quote_identity=identity)
        record_path = Path(dim_cache._cache_path("300398", "intel"))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record.pop("quote_identity", None)
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        assert dim_cache.load_cached_dim("300398", "intel") is None, "双缺身份不得 hit"
        assert dim_cache.load_cached_dim("300398", "intel", quote_identity=identity) is None

    def test_old_intel_contract_misses_without_global_invalidation(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旧情报契约（无 intel_contract_version / 版本不符）重算；非 intel 维度不受影响。"""
        import os

        self._prepare(tmp_path, monkeypatch)
        os.makedirs(dim_cache._CACHE_DIR, exist_ok=True)
        stale = {
            "schema_version": dim_cache.SCHEMA_VERSION,
            "source_label_version": dim_cache.SOURCE_LABEL_VERSION,
            "saved_at": "2026-10-30T15:00:00",
            "stock_code": "300398",
            "dim": "intel",
            "payload": _intel_payload(),
        }
        for version in (None, 1):
            record = dict(stale)
            if version is not None:
                record["intel_contract_version"] = version
                record["quote_identity"] = {"absent": True}
            with open(dim_cache._cache_path("300398", "intel"), "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False)
            assert dim_cache.load_cached_dim("300398", "intel", quote_identity={"absent": True}) is None, (
                "旧契约 intel 缓存必须 miss 重算"
            )
        # 非 intel 维度（data）不带身份语义，保存/读取行为不变
        dim_cache.save_cached_dim("300398", "data", DIM_MODELS["data"]().model_dump())
        assert dim_cache.load_cached_dim("300398", "data") is not None


# ---------------------------------------------------------------------------
# 契约 2/4 垂直链：真实编排 → 采用/缓存 → 成文 → 保存 → API/SSE → MD/PDF
# ---------------------------------------------------------------------------

_FLYKET_INTEL_WITH_TRADES = {
    "sentiment_summary": "公告与行业新闻中性偏正面",
    "earnings_outlook": "订单回暖",
    "risk_alerts": ["原料价格波动"],
    "positive_catalysts": ["下游补库"],
    "latest_news": "披露订单回暖公告",
    "unverified_count": 0,
    "evidence_items": [
        {"evidence": "券商维持增持评级，目标价 29.4 元（news 级二手）", "strength": "weak_positive", "lr": 1.2, "date": "2026-10-30"},
        {"evidence": "券商维持买入评级，目标价29.4元（news级二手）", "strength": "weak_positive", "lr": 1.2, "date": "2026-10-30"},
    ],
    "root_cause": {"event": "订单回暖", "mechanism": "下游补库", "magnitude": "正面利好", "persistence": "中期"},
    "event_calendar": [
        {
            "event": "年度业绩预告",
            "date": "2027-01-31",
            "outcomes": ["净利超预期", "净利符合预期"],
            "plans": [
                "T1-R 右侧确认买入：确认净利达标后于放量收复 36 元时建仓，仓位 ≤3%",
                "T1-SL 止损：跌破 32 元无条件清仓离场（一票否决）",
                "披露后对照公告原文核验净利 ≥5.5 亿元且毛利率 ≥40% 的阈值",
                '核验券商维持"买入"评级、目标价29.4元的研报原文',
            ],
        }
    ],
}


class _TradePlanIntelAdapter(_DeterministicResearchAdapter):
    """情报研究返回独立 36/32 元交易预案（冻结反例形态）；其余消费沿用父类。"""

    def __init__(self) -> None:
        self.intel_calls = 0
        self.intel_user_messages: List[str] = []
        self.manager_user_messages: List[str] = []

    def _explore_content(self, user: str) -> str:
        if "情报收集" in user:
            self.intel_calls += 1
            return json.dumps(_FLYKET_INTEL_WITH_TRADES, ensure_ascii=False)
        return super()._explore_content(user)

    def call_with_tools(
        self, messages: List[Dict[str, Any]], tool_decls: List[Any], timeout: Any = None,
    ) -> LLMResponse:
        user = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
        if "情报收集" in user:
            self.intel_user_messages.append(user)
        return super().call_with_tools(messages, tool_decls, timeout)

    def call_text(self, messages: List[Dict[str, Any]], **kwargs: Any) -> LLMResponse:
        system = str(next((m.get("content") or "") for m in messages if m.get("role") == "system"))
        if "主笔基金经理" in system:
            self.manager_user_messages.append(
                str(next((m.get("content") or "") for m in messages if m.get("role") == "user"))
            )
        return super().call_text(messages, **kwargs)


def test_trade_directive_plans_rejected_across_vertical_chain(
    tmp_path, monkeypatch, choice_transport,
) -> None:
    """独立 36/32 元买入预案在采用边界拒绝：JSON/MD/PDF/API 不含可执行指令，
    拒绝原因随「内容限制披露」可见；合法事件、研报引用与核验动作保留。"""
    import io

    from fastapi.testclient import TestClient
    from pypdf import PdfReader
    from src.services import deep_research_service as service_module

    service, database, app, _transport = _no_target_vertical_env(
        tmp_path, monkeypatch, choice_transport,
    )
    adapter = _TradePlanIntelAdapter()
    monkeypatch.setattr(service_module, "_get_dual_track_adapter", lambda: adapter)
    try:
        with TestClient(app) as client:
            stream = client.post(
                "/generate/stream",
                json={
                    "stock_code": "300398", "stock_name": "飞凯材料",
                    "dims": _NO_TARGET_RESEARCH_DIMS, "force_refresh": True,
                },
            )
            assert stream.status_code == 200, stream.text
            events = _sse_events(stream)
            done = _assert_single_done_terminal(events)
            rid = done["report_id"]
            assert done["status"] == "success", (
                done["status"], done.get("error"),
            )

            # 契约 1：真实情报出站消息携带本报告已采用行情（fixture 现价 20.0）
            assert adapter.intel_user_messages, "情报出站消息未被捕获"
            intel_message = adapter.intel_user_messages[0]
            assert "现价 20.0" in intel_message
            assert "事件预案编号联动" not in intel_message
            assert "左侧买入" not in intel_message and "右侧确认买入" not in intel_message
            # 契约 3：经理波 C 市场依据与波 A 同源
            wave_a = json.loads(adapter.manager_user_messages[0])
            wave_c = json.loads(adapter.manager_user_messages[2])
            assert wave_c["completed_research"]["market"]["current_price"] == (
                wave_a["completed_research"]["market"]["current_price"]
            )

            intel = done["dimensions"]["intel"]
            plans = [p for ev in intel["event_calendar"] for p in ev["plans"]]
            assert plans == [
                "披露后对照公告原文核验净利 ≥5.5 亿元且毛利率 ≥40% 的阈值",
                '核验券商维持"买入"评级、目标价29.4元的研报原文',
            ], plans
            assert "T1-R" not in json.dumps(intel, ensure_ascii=False)
            assert "36 元" not in json.dumps(intel, ensure_ascii=False)
            assert "32 元" not in json.dumps(intel, ensure_ascii=False)
            assert any("未采用" in note for note in intel["truncation_notes"])
            # 合法研报引用不被无差别删除（含 spec 反例的无引号「买入」评级证据）
            assert any("增持" in e["evidence"] for e in intel["evidence_items"])
            assert any(
                "券商维持买入评级，目标价29.4元" in e["evidence"]
                for e in intel["evidence_items"]
            ), "无引号评级引用证据必须进入贝叶斯证据链"

            markdown = done["markdown"]
            assert "收复 36 元" not in markdown and "跌破 32 元" not in markdown
            assert "一票否决" not in markdown
            # 规则作战计划的 T1 编号是报告唯一交易指令来源（合法保留），
            # 情报事件表区域不得再出现独立指令
            assert "未采用" in markdown, "拒绝原因随内容限制披露可见"
            # plan 维度正常：事件表尾注引用规则作战计划（非「未生成」分支）
            assert "操作条件以报告规则作战计划为准" in markdown
            assert "未生成有效规则计划" not in markdown
            assert "研究核验/跟踪" in markdown
            assert "披露后对照公告原文核验净利 ≥5.5 亿元" in markdown
            assert "增持评级" in markdown
            assert '维持"买入"评级' in markdown, "ASCII 引号评级引用随事件表成文保留"
            assert "维持买入评级" in markdown, "无引号评级引用证据随成文保留"

            data = client.get(f"/reports/{rid}").json()["data"]
            saved_intel = data["dimensions"]["intel"]
            assert saved_intel["event_calendar"] == intel["event_calendar"]
            assert any(
                "券商维持买入评级，目标价29.4元" in e["evidence"]
                for e in saved_intel["evidence_items"]
            ), "反例证据经保存 JSON 不丢"
            assert any("未采用" in note for note in saved_intel["truncation_notes"])

            pdf = client.get(f"/reports/{rid}/pdf")
            assert pdf.status_code == 200, pdf.text
            pdf_text = "".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf.content)).pages)
            assert "收复 36 元" not in pdf_text and "跌破 32 元" not in pdf_text
            assert "未采用" in pdf_text
    finally:
        database._engine.dispose()


_FLYKET_INTEL_STRING_PLANS = {
    **_FLYKET_INTEL_WITH_TRADES,
    "event_calendar": [
        {
            "event": "年度业绩预告",
            "date": "2027-01-31",
            "outcomes": ["净利超预期", "净利符合预期"],
            # 2026-10-30 飞凯材料真实响应形态：整个 plans 槽位是单个完整字符串
            "plans": "对照巨潮资讯公告原文核验预告中的营收/净利润区间与变动原因，并与三季报趋势交叉验证",
        },
        {
            "event": "光通信材料订单波动",
            "date": "未知",
            "outcomes": ["订单加速", "订单放缓"],
            # 字符串形态的独立交易指令：整条拒绝，不得按字符拆散绕过
            "plans": "T1-R 右侧确认买入：确认净利达标后于放量收复 36 元时建仓，仓位 ≤3%",
        },
    ],
}


class _StringPlanIntelAdapter(_TradePlanIntelAdapter):
    """plans 槽位为单个完整字符串（真实观察形态）；其余消费沿用父类。"""

    def _explore_content(self, user: str) -> str:
        if "情报收集" in user:
            self.intel_calls += 1
            return json.dumps(_FLYKET_INTEL_STRING_PLANS, ensure_ascii=False)
        return super()._explore_content(user)


def test_string_form_plans_adopted_whole_and_trade_string_rejected(
    tmp_path, monkeypatch, choice_transport,
) -> None:
    """真实报告字符串形态贯通垂直链：合法单字符串预案完整进入采用/缓存/保存/
    MD；单字符串交易指令整条拒绝并披露，字符拆散残渣与指令片段不出现在任何
    持久化产物（真实 2026-10-30 报告按字符拆散并伪造丢弃 33 条的缺陷回归）。"""
    from fastapi.testclient import TestClient
    from src.services import deep_research_service as service_module

    service, database, app, _transport = _no_target_vertical_env(
        tmp_path, monkeypatch, choice_transport,
    )
    adapter = _StringPlanIntelAdapter()
    monkeypatch.setattr(service_module, "_get_dual_track_adapter", lambda: adapter)
    try:
        with TestClient(app) as client:
            stream = client.post(
                "/generate/stream",
                json={
                    "stock_code": "300398", "stock_name": "飞凯材料",
                    "dims": _NO_TARGET_RESEARCH_DIMS, "force_refresh": True,
                },
            )
            assert stream.status_code == 200, stream.text
            done = _assert_single_done_terminal(_sse_events(stream))
            rid = done["report_id"]
            assert done["status"] == "success", (done.get("status"), done.get("error"))
            intel = done["dimensions"]["intel"]
            events = intel["event_calendar"]
            assert len(events) == 2
            legit = "对照巨潮资讯公告原文核验预告中的营收/净利润区间与变动原因，并与三季报趋势交叉验证"
            # 合法单字符串预案完整保留为一条，不按字符拆散
            assert events[0]["plans"] == [legit]
            assert events[0]["outcomes"] == ["净利超预期", "净利符合预期"]
            # 字符串形态交易指令整条拒绝并披露
            assert events[1]["plans"] == []
            assert any("未采用" in note for note in intel["truncation_notes"])
            assert all(len(plan) > 1 for ev in events for plan in ev["plans"]), (
                "不得出现按字符拆散的条目"
            )

            payload_text = json.dumps(intel, ensure_ascii=False)
            assert "T1-R" not in payload_text and "36 元" not in payload_text
            assert legit in payload_text

            markdown = done["markdown"]
            assert legit in markdown, "完整字符串预案随事件表成文保留"
            assert "收复 36 元" not in markdown
            assert "未采用" in markdown, "拒绝原因随内容限制披露可见"

            data = client.get(f"/reports/{rid}").json()["data"]
            saved_intel = data["dimensions"]["intel"]
            assert saved_intel["event_calendar"] == intel["event_calendar"]
            assert any("未采用" in note for note in saved_intel["truncation_notes"])
    finally:
        database._engine.dispose()


def test_intel_cache_honors_quote_identity_on_real_orchestration(
    tmp_path, monkeypatch, choice_transport,
) -> None:
    """真实 try_cache/store_cache（绕过服务层报告日缓存，直接走编排器）：
    同身份第二次编排命中情报缓存不再研究；变价后保守 miss 重算。
    fixture 行情带 field_meta 以形成有效身份。"""
    from datetime import date

    import pandas as pd
    from src.agent.deep_research import orchestrator
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.services import history_loader

    adapter = _TradePlanIntelAdapter()
    today = date.today().isoformat()

    def _deterministic_history(
        stock_code: str, days: int = 60, target_date: Any = None, min_records: Any = None,
    ) -> Any:
        rows = 1200
        idx = pd.bdate_range(end=today, periods=rows)
        return pd.DataFrame({
            "date": idx.strftime("%Y-%m-%d"),
            "open": [10.0] * rows,
            "high": [11.0] * rows,
            "low": [9.0] * rows,
            "close": [10.0 + i * 0.01 for i in range(rows)],
            "volume": [1000] * rows,
        }), "fixture_db"

    monkeypatch.setattr(history_loader, "load_history_df", _deterministic_history)
    quote_state: Dict[str, Any] = {"value": {
        "price": 20.0,
        "field_meta": {"current_price": {
            "source": "fixture", "observed_at": f"{today} 15:00:03",
            "rule_eligible": True, "input_reasons": [],
        }},
    }}

    def _build_context(*args: Any, **kwargs: Any) -> SharedContext:
        ctx = SharedContext("300398", "飞凯材料", f"{today}T15:00:00", quote=quote_state["value"])
        ctx.history = [
            {"date": f"2026-09-{day:02d}", "open": 10, "high": 11, "low": 9, "close": 10.0 + day * 0.05, "volume": 1000}
            for day in range(1, 29)
        ]
        ctx.fundamental = _safe_fundamental("300398", ctx)
        return ctx

    monkeypatch.setattr(orchestrator, "build_shared_context", _build_context)

    def _intel_plans(result: Any) -> List[str]:
        return [p for ev in result.dims["intel"].event_calendar for p in ev.plans]

    first = orchestrator.run_dual_track(
        "300398", "飞凯材料", adapter, force_refresh=True,
        dims_filter=set(_NO_TARGET_RESEARCH_DIMS), report_id=None,
    )
    assert first.status == "success", first.error
    assert adapter.intel_calls == 1
    # 变价前缓存写入身份 A（quote 有 field_meta，非身份不足）
    assert adapter.intel_user_messages and "现价 20.0" in adapter.intel_user_messages[0]
    first_plans = _intel_plans(first)
    assert first_plans and all(
        "披露后对照公告原文" in p or "维持\"买入\"评级" in p for p in first_plans
    ), first_plans

    # 同身份：情报维度缓存命中，不重新研究
    second = orchestrator.run_dual_track(
        "300398", "飞凯材料", adapter, force_refresh=False,
        dims_filter=set(_NO_TARGET_RESEARCH_DIMS), report_id=None,
    )
    assert second.status == "success", second.error
    assert adapter.intel_calls == 1, "同行情身份应命中情报缓存"
    assert _intel_plans(second) == first_plans

    # 变价：保守 miss，情报重算（真实研究路径，非全局关缓存）
    quote_state["value"] = {
        "price": 21.5,
        "field_meta": {"current_price": {
            "source": "fixture", "observed_at": f"{today} 15:00:03",
            "rule_eligible": True, "input_reasons": [],
        }},
    }
    third = orchestrator.run_dual_track(
        "300398", "飞凯材料", adapter, force_refresh=False,
        dims_filter=set(_NO_TARGET_RESEARCH_DIMS), report_id=None,
    )
    assert third.status == "success", third.error
    assert adapter.intel_calls == 2, "已采用行情变价后情报必须重算"
    assert _intel_plans(third) == first_plans, "拒绝契约在重算路径同样生效"
    assert "现价 21.5" in adapter.intel_user_messages[-1]


def test_no_rule_plan_report_keeps_rejection_without_substitute_points(
    tmp_path, monkeypatch, choice_transport,
) -> None:
    """无规则计划（plan 维度未选）时：拒绝披露不伪造替代交易点位。"""
    from fastapi.testclient import TestClient
    from src.services import deep_research_service as service_module

    service, database, app, _transport = _no_target_vertical_env(
        tmp_path, monkeypatch, choice_transport,
    )
    adapter = _TradePlanIntelAdapter()
    monkeypatch.setattr(service_module, "_get_dual_track_adapter", lambda: adapter)
    dims_without_plan = [d for d in _NO_TARGET_RESEARCH_DIMS if d != "plan"]
    try:
        with TestClient(app) as client:
            stream = client.post(
                "/generate/stream",
                json={
                    "stock_code": "300398", "stock_name": "飞凯材料",
                    "dims": dims_without_plan, "force_refresh": True,
                },
            )
            assert stream.status_code == 200, stream.text
            done = _assert_single_done_terminal(_sse_events(stream))
            assert done["status"] == "success", (done.get("status"), done.get("error"))
            intel = done["dimensions"]["intel"]
            plans = [p for ev in intel["event_calendar"] for p in ev["plans"]]
            # 合法财务核验阈值与评级研报核验保留（都不是交易点位）；独立指令已拒绝
            assert plans == [
                "披露后对照公告原文核验净利 ≥5.5 亿元且毛利率 ≥40% 的阈值",
                '核验券商维持"买入"评级、目标价29.4元的研报原文',
            ]
            markdown = done["markdown"]
            assert "收复 36 元" not in markdown and "跌破 32 元" not in markdown
            assert "未采用" in markdown
            # plan 维度未选择：事件表尾注如实说明未生成有效规则计划，
            # 不给替代交易点位，也不暗示不存在的计划
            assert "未生成有效规则计划" in markdown
            assert "不含交易点位" in markdown
    finally:
        database._engine.dispose()
