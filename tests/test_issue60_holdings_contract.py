# -*- coding: utf-8 -*-
"""Issue #60 回归：公共资料指标语义与适用记录选择契约（离线，无新供应商请求）。

修复前失败的契约反例，修复后必须全部通过：
- 「股东户数-增减」不再是机构/十大股东持仓变化、方向或评分依据；
- 适用记录按证券身份与指标自身业务期间选择，与供应商行序无关；
- 无期间证据、日期字段冲突、同期值歧义保持明确缺口，不编造「最新」；
- 观察时间语义排除未来交易日，预告目标期的未来记录合法保留；
- 公共资金流取最新交易日的值与观察时间，机构缺口不牵连资金侧解释；
- 旧缓存/旧载荷中的错映射值保留可读，但不获得合格资格（不升级）。
"""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from datetime import date, timedelta
from typing import Any, Optional
from unittest.mock import patch

import akshare
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import AkshareFundamentalAdapter, _extract_latest_row

from tests.test_single_pass_pilots import _success_model  # noqa: E402
from tests.test_choice_capital_flow import native_flow  # noqa: E402,F401


def _household_frame(order: str = "asc") -> pd.DataFrame:
    """真实端点形状（issue #60 实测 63 行表的脱敏三行版）：正序 2013→2026。"""
    rows = [
        {"代码": "300260", "股东户数统计截止日": "2013-03-31", "股东户数-增减": -1323},
        {"代码": "300260", "股东户数统计截止日": "2019-06-30", "股东户数-增减": 4021},
        {"代码": "300260", "股东户数统计截止日": "2026-09-30", "股东户数-增减": 17},
    ]
    if order == "desc":
        rows.reverse()
    elif order == "shuffled":
        rows = [rows[2], rows[0], rows[1]]
    return pd.DataFrame(rows)


class TestApplicableRecordSelection:
    """适用记录选择：身份 + 指标自身业务期间，行序不参与。"""

    def test_row_order_never_decides_selection(self) -> None:
        for order in ("asc", "desc", "shuffled"):
            row = _extract_latest_row(_household_frame(order), "300260")
            assert row is not None, order
            assert row["股东户数统计截止日"] == "2026-09-30", order
            assert row["股东户数-增减"] == 17, order

    def test_other_security_yields_no_record(self) -> None:
        assert _extract_latest_row(_household_frame(), "600519") is None

    def test_identity_columns_must_agree(self) -> None:
        df = pd.DataFrame([
            {"代码": "300260", "证券代码": "300260", "报告期": "2026-09-30", "值": 1},
            {"代码": "300260", "证券代码": "000001", "报告期": "2026-09-30", "值": 2},
        ])
        row = _extract_latest_row(df, "300260")
        assert row is not None
        assert row["值"] == 1  # 只有两条身份列都指向 300260 的记录可采

    def test_missing_period_column_is_a_gap(self) -> None:
        assert _extract_latest_row(pd.DataFrame([{"代码": "300260", "值": 1}]), "300260") is None

    def test_unparseable_dates_are_not_recency_evidence(self) -> None:
        df = pd.DataFrame([
            {"代码": "300260", "报告期": "未知", "值": 1},
            {"代码": "300260", "报告期": "2026-09-30", "值": 2},
        ])
        row = _extract_latest_row(df, "300260")
        assert row is not None
        assert row["值"] == 2
        assert _extract_latest_row(df.drop(index=1), "300260") is None

    def test_announce_date_never_replaces_statistics_period(self) -> None:
        # 公告日期新但统计期旧：业务时点以统计期为准，不借公告日冒充最新。
        df = pd.DataFrame([
            {"代码": "300260", "报告期": "2013-03-31", "公告日期": "2026-10-27", "值": 1},
            {"代码": "300260", "报告期": "2026-09-30", "公告日期": "2026-10-20", "值": 2},
        ])
        row = _extract_latest_row(df, "300260")
        assert row is not None
        assert row["值"] == 2

    def test_record_with_self_contradicting_period_is_ineligible(self) -> None:
        df = pd.DataFrame([
            {"代码": "300260", "报告期": "2026-09-30", "交易日": "2013-03-31", "值": 1},
        ])
        assert _extract_latest_row(df, "300260") is None

    def test_same_period_conflicting_values_are_a_gap_and_duplicates_are_fine(self) -> None:
        conflicting = pd.DataFrame([
            {"代码": "300260", "报告期": "2026-09-30", "值": 1},
            {"代码": "300260", "报告期": "2026-09-30", "值": 2},
        ])
        assert _extract_latest_row(conflicting, "300260") is None
        duplicated = pd.concat([conflicting.iloc[[0]], conflicting.iloc[[0]]], ignore_index=True)
        row = _extract_latest_row(duplicated, "300260")
        assert row is not None
        assert row["值"] == 1

    def test_future_observation_excluded_but_future_forecast_target_kept(self) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # 已发生语义（快报/资金流默认）：未来期间不是已发生数据，不作为候选。
        trades = pd.DataFrame([
            {"代码": "300260", "日期": tomorrow, "值": 99},
            {"代码": "300260", "日期": yesterday, "值": 7},
        ])
        row = _extract_latest_row(trades, "300260")
        assert row is not None
        assert row["值"] == 7
        # 预告目标期语义：未来报告期的预告是合法记录，按其目标期当选。
        forecasts = pd.DataFrame([
            {"代码": "300260", "预告期": yesterday, "值": 1},
            {"代码": "300260", "预告期": tomorrow, "值": 2},
        ])
        row = _extract_latest_row(forecasts, "300260", allow_future_target=True)
        assert row is not None
        assert row["值"] == 2

    def test_zero_is_a_valid_observed_value(self) -> None:
        df = pd.DataFrame([
            {"代码": "300260", "日期": "2026-10-26", "值": 5},
            {"代码": "300260", "日期": "2026-10-27", "值": 0},
        ])
        row = _extract_latest_row(df, "300260")
        assert row is not None
        assert row["值"] == 0  # 最新交易日的 0 是合法观察值，不因零值回退旧行

    def test_without_identity_column_a_record_cannot_claim_the_stock(self) -> None:
        # 供应商 unscoped 默认返回可能属于别的股票：无行级代码列一律不采。
        df = pd.DataFrame([{"日期": "2026-10-27", "值": 5}])
        assert _extract_latest_row(df, "300260") is None

    def test_same_period_revision_picked_by_latest_disclosure(self) -> None:
        # 同统计期的修订链：按披露时间取最新修订，统计期保持不变。
        df = pd.DataFrame([
            {"代码": "300260", "报告期": "2026-09-30", "公告日期": "2026-10-25", "值": "最新上修预告"},
            {"代码": "300260", "报告期": "2026-09-30", "公告日期": "2026-10-20", "值": "较早预告"},
        ])
        row = _extract_latest_row(df, "300260", allow_future_target=True)
        assert row is not None
        assert row["值"] == "最新上修预告"

    def test_publication_date_alone_never_qualifies_as_period(self) -> None:
        # 只有代码+公告日期而无报告期/交易日：披露时间不构成业务期间资格，保留缺口。
        df = pd.DataFrame([{"代码": "300260", "公告日期": "2026-10-27", "值": 1}])
        assert _extract_latest_row(df, "300260") is None


class TestPublicBundleContract:
    """从公共适配真实入口验证（mock 传输边界，无网络请求）。"""

    def test_household_delta_never_becomes_holding_change(self) -> None:
        with ExitStack() as stack:
            for name in (
                "stock_yjyg_em", "stock_yjkb_em", "stock_fhps_detail_em",
                "stock_history_dividend_detail", "stock_dividend_cninfo",
                "stock_institute_hold",
            ):
                if hasattr(akshare, name):
                    stack.enter_context(patch.object(akshare, name, return_value=pd.DataFrame()))
            gdhs_mock = stack.enter_context(
                patch.object(akshare, "stock_zh_a_gdhs_detail_em", return_value=_household_frame())
            )
            inst_mock = stack.enter_context(
                patch.object(akshare, "stock_institute_hold", return_value=pd.DataFrame())
            )
            result = AkshareFundamentalAdapter().get_fundamental_bundle("300260", financials={
                "growth": {}, "valuation": {}, "earnings": {}, "institution": {}, "errors": [], "source_chain": [],
            })
        gdhs_mock.assert_not_called()  # 股东户数接口不再被当作持仓资料调用
        inst_mock.assert_not_called()  # 隐式 20051 季度接口不再提供持仓变化
        assert result["institution"] == {}
        assert "top10_holder_change" not in result["institution"]
        assert "institution_holding_change" not in result["institution"]
        assert not any("gdhs" in chain or "institute_hold" in chain for chain in result["source_chain"])

    def test_forecast_summary_kept_with_own_period_evidence(self) -> None:
        adapter = AkshareFundamentalAdapter()

        def transport(self: Any, candidates: Any) -> tuple[Optional[pd.DataFrame], str, list[str]]:
            if candidates[0][0] == "stock_yjyg_em":
                return (
                    pd.DataFrame([{"代码": "300260", "预告期": "2025-12-31", "预告": "预增"}]),
                    "stock_yjyg_em",
                    [],
                )
            return None, "fixture_public", []

        with patch.object(AkshareFundamentalAdapter, "_call_df_candidates", transport):
            result = adapter.get_fundamental_bundle("300260", financials={
                "growth": {}, "valuation": {}, "earnings": {}, "institution": {}, "errors": [], "source_chain": [],
            })
        assert result["earnings"]["forecast_summary"] == "预增"

    def test_capital_flow_adopts_latest_trading_day(self) -> None:
        adapter = AkshareFundamentalAdapter()
        # 真实 scoped SDK 形状：逐股资金流历史无行级代码列（stock_fund_em.py），归属由
        # stock+market 请求保证；正序历史表的值与观察时间都取最新交易日。
        df = pd.DataFrame([
            {"日期": "2026-10-26", "主力净流入-净额": "-500.0"},
            {"日期": "2026-10-27", "主力净流入-净额": "1200.0"},
        ])
        calls: list[list[tuple[str, dict[str, Any]]]] = []

        def transport(candidates: list[tuple[str, dict[str, Any]]]):
            calls.append(candidates)
            if candidates[0][0] == "stock_individual_fund_flow":
                assert candidates[0][1] == {"stock": "300260", "market": "sz"}
                return df, "stock_individual_fund_flow", []
            return None, "fixture_public", []

        with patch.object(adapter, "_call_df_candidates", side_effect=transport):
            result = adapter.get_capital_flow("300260")
        assert calls and calls[0][0][0] == "stock_individual_fund_flow"  # scoped 候选优先
        assert result["stock_flow"]["main_net_inflow"] == 1200.0
        meta = result["stock_flow"]["field_meta"]["main_inflow"]
        assert meta["observed_at"] == "2026-10-27"

    def test_scoped_response_with_conflicting_code_stays_rejected(self) -> None:
        # 供应商已有行级代码列时按行级证据过滤，请求归属不得覆盖身份冲突。
        adapter = AkshareFundamentalAdapter()
        df = pd.DataFrame([{"股票代码": "600519", "日期": "2026-10-28", "主力净流入-净额": "12.0"}])

        def transport(candidates: list[tuple[str, dict[str, Any]]]):
            if candidates[0][0] == "stock_individual_fund_flow":
                return df, "stock_individual_fund_flow", []
            return None, "fixture_public", []

        with patch.object(adapter, "_call_df_candidates", side_effect=transport):
            result = adapter.get_capital_flow("300260")
        assert result["stock_flow"].get("main_net_inflow") is None

    def test_old_mapping_snapshot_cannot_enter_new_report(self, monkeypatch, tmp_path) -> None:
        """旧 v18 阶段 0 快照中的户数错映射不得进入新报告（真实 dim_cache 磁盘边界）。"""
        from types import SimpleNamespace

        from src.config import Config
        from src.deep_research_dims import dim_cache
        from src.deep_research_dims.context import SharedContext, _safe_fundamental
        from src.agent.tools import data_tools
        from data_provider.mx_data_adapter import financial_refresh

        cfg = Config(data_source_profile="legacy", deep_research_cross_validate=False,
                     enable_fuyao=False, enable_ifind_mcp=False)
        monkeypatch.setattr(Config, "_instance", cfg)
        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
        old = {key: 1.0 for key in ("pe_ttm", "pb", "roe", "gross_margin", "revenue_growth", "net_profit_yoy", "market_cap")}
        old["institution"] = {"status": "partial", "data": {"top10_holder_change": -1323.0}, "field_meta": {}}
        dim_cache.save_snapshot(f"stage0_fund_{cfg.data_source_contract}_v18_cv0_300260", old)
        calls: list[str] = []

        def current(code: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(code)
            return {"stock_code": code, "growth": {"status": "missing", "data": {}},
                    "valuation": {"status": "missing", "data": {}},
                    "institution": {"status": "missing", "data": {}}, "errors": []}

        monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: SimpleNamespace(get_fundamental_context=current))
        token = financial_refresh.set(None)
        try:
            result = _safe_fundamental("300260", SharedContext("300260", "fixture", "2026-10-28"))
        finally:
            financial_refresh.reset(token)
        assert calls == ["300260"], result  # 旧 v18 映射键不命中：按当前契约重新取数
        assert "top10_holder_change" not in (result.get("institution", {}).get("data", {})), result


def _bind_run(monkeypatch: pytest.MonkeyPatch, institution_block: dict[str, Any]) -> Any:
    """run_capital_agent 的离线环境：Choice 冻结资金流 + 共享上下文既有机构块。

    issue #64：机构块只是阶段0既有公司资料——资金研究员不读取、不查询
    （get_fundamental_context 保持零调用守卫）；资金流继续走 native_flow 冻结传输。
    """
    from src.config import Config
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext
    from src.agent.tools import data_tools

    config = Config(data_source_profile="choice", deep_research_cross_validate=False)
    monkeypatch.setattr(Config, "_instance", config)
    manager = data_tools._get_fetcher_manager()

    def no_institution(code: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("机构块取数已退役，不得调用 get_fundamental_context")

    monkeypatch.setattr(manager, "get_fundamental_context", no_institution)
    run = ResearchRun(timeout_seconds=60.0)
    run.context = SharedContext("300260", "新莱应材", "2026-10-28")
    run.context.fundamental = {"institution": institution_block}
    return run


class TestCapitalResearcherQuadrants:
    """issue #64 资金面流专用：合格三窗口决定完成与评分，机构块不再参与。"""

    _QUALIFIED_INSTITUTION = {
        "status": "ok",
        "data": {"institution_holding_change": -1.25},
        "field_meta": {"institution_holding_change": {"rule_eligible": True, "period": "2026中报"}},
    }

    def test_qualified_flow_completes_and_discards_institution_output(self, native_flow, monkeypatch) -> None:
        """资金合格即完成：score = 合格 flow_score；模型输出的机构/合成字段一律丢弃。"""
        from src.agent.deep_research import explore_agents

        run = _bind_run(monkeypatch, dict(self._QUALIFIED_INSTITUTION))
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model(
            '{"flow_summary": "主力净流入为正", "flow_score": 72, "institution_summary": "机构减持",'
            ' "institution_score": 55, "score": 63, "narrative": "n"}', calls_model))
        with run.bind():
            result = explore_agents.run_capital_agent("300260", "新莱应材", object())
        assert result["ok"] is True
        data = result["data"]
        assert data.get("status") != "degraded"  # 资金合格：不降级
        assert data["flow_score"] == 72 and data["score"] == 72  # 资金总分 = flow_score，旧合成 63 作废
        assert data.get("institution_score") is None and data.get("institution_summary") is None

    def test_flow_unqualified_starts_no_model(self, native_flow, monkeypatch) -> None:
        """资金侧不合格即不启动模型；缺口只说资金，机构侧不再参与门控或文案。"""
        from src.agent.deep_research import explore_agents

        calls, dates = native_flow
        dates.remove("2026-10-13")  # 最新交易日无数据 → 资金侧金额 None
        unqualified = {
            "status": "partial",
            "data": {"institution_holding_change": -1323.0},
            "field_meta": {"institution_holding_change": {"rule_eligible": False, "input_reasons": ["unit_unknown"]}},
        }
        run = _bind_run(monkeypatch, unqualified)
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model("{}", calls_model))
        with run.bind():
            result = explore_agents.run_capital_agent("300260", "新莱应材", object())
        assert calls_model == []  # 资金不合格：不启动模型
        assert result["ok"] is False
        assert "未启动模型" in result["error"]
        assert "资金" in result["error"] and "机构" not in result["error"]

    def test_public_adapter_output_feeds_researcher_as_true_gap(self, native_flow, monkeypatch, tmp_path) -> None:
        """真实公共 manager 聚合产物与资金研究员的边界：机构缺口块留在既有公司
        资料里，不进入资金任务、评分或降级语义。

        阶段一用 legacy profile 的真实 DataFetcherManager.get_fundamental_context
        聚合公共传输（仅 mock 外部传输），阶段二把同一真实产物作为共享上下文
        供给研究员（Choice 资金侧走 native_flow 冻结传输）。
        """
        from src.agent.deep_research import explore_agents
        from src.config import Config
        from src.core.research_run import ResearchRun
        from src.deep_research_dims import dim_cache
        from src.deep_research_dims.context import SharedContext
        from data_provider.base import DataFetcherManager

        # 阶段一：真实公共聚合（legacy profile，无 choice/tushare/fuyao 依赖）。
        public_cfg = Config(data_source_profile="legacy", deep_research_cross_validate=False,
                            enable_fuyao=False, enable_ifind_mcp=False, fundamental_cache_ttl_seconds=0)
        monkeypatch.setattr(Config, "_instance", public_cfg)
        monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))

        called_candidates: list[str] = []

        def public_transport(self: Any, candidates: list[tuple[str, dict[str, Any]]]) -> tuple[Optional[pd.DataFrame], Optional[str], list[str]]:
            name = candidates[0][0]
            called_candidates.append(name)
            if name == "stock_individual_fund_flow":
                # 真实 scoped SDK 形状：无行级代码列，归属由 stock+market 请求保证。
                return pd.DataFrame([
                    {"日期": "2026-10-26", "主力净流入-净额": "-500.0"},
                    {"日期": "2026-10-27", "主力净流入-净额": "1200.0"},
                ]), "stock_individual_fund_flow", []
            return None, "fixture_public", []

        with ExitStack() as stack:
            for name in (
                "stock_yjyg_em", "stock_yjkb_em", "stock_fhps_detail_em",
                "stock_history_dividend_detail", "stock_dividend_cninfo",
                "stock_institute_hold", "stock_main_fund_flow",
                "stock_sector_fund_flow_rank", "stock_sector_fund_flow_summary",
            ):
                if hasattr(akshare, name):
                    stack.enter_context(patch.object(akshare, name, return_value=pd.DataFrame()))
            # 错映射源头即使有返回也不再产生持仓字段：户数表照常返回。
            stack.enter_context(patch.object(akshare, "stock_zh_a_gdhs_detail_em", return_value=_household_frame()))
            stack.enter_context(patch.object(AkshareFundamentalAdapter, "_call_df_candidates", public_transport))
            manager = DataFetcherManager()
            context = manager.get_fundamental_context("300260", budget_seconds=20)
        assert "stock_individual_fund_flow" in called_candidates  # scoped 资金传输真实被调用
        # 公共资金正向值真实进入 context，而非空块假绿。
        flow_block = context.get("capital_flow") or {}
        assert (flow_block.get("data") or {}).get("stock_flow", {}).get("main_net_inflow") == 1200.0
        institution_block = context.get("institution")
        assert isinstance(institution_block, dict)
        assert institution_block["data"].get("top10_holder_change") is None
        assert institution_block["data"].get("institution_holding_change") is None

        # 阶段二：真实产物作为共享上下文供给研究员（Choice 资金侧冻结传输）。
        choice_cfg = Config(data_source_profile="choice", deep_research_cross_validate=False)
        monkeypatch.setattr(Config, "_instance", choice_cfg)
        run = ResearchRun(timeout_seconds=60.0)
        run.context = SharedContext("300260", "新莱应材", "2026-10-28")
        run.context.fundamental = {"institution": institution_block}
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model(
            '{"flow_summary": "主力净流入为正", "flow_score": 72, "institution_summary": "线索",'
            ' "institution_score": null, "score": null, "narrative": "资金侧解读。"}', calls_model))
        with run.bind():
            result = explore_agents.run_capital_agent("300260", "新莱应材", object())
        assert result["ok"] is True
        data = result["data"]
        # issue #64：机构缺口块不再牵连资金完成语义——资金合格即完成
        assert data.get("status") != "degraded"
        assert data["flow_score"] == 72 and data["score"] == 72  # 资金总分 = 合格 flow_score
        assert data.get("institution_summary") is None and data.get("institution_score") is None
        task_text = calls_model[0]["messages"][-1]["content"]
        assert "-1323" not in task_text  # 户数增减不再作为持仓证据进入模型输入
        assert "institution_holding_change" not in task_text  # 机构缺口块不进入资金任务
