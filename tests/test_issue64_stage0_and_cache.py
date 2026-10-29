# -*- coding: utf-8 -*-
"""Issue #64 切片6：阶段0零行业榜出站、缓存 v15 边界、产物 18 项、报告日缓存 scope。

规格：从阶段0与公共取数入口验证行业榜/机构侧零出站；缓存契约 14→15（旧机构/
治理分、旧退役缺口不 bleed）；capital 缓存采用核对实际截至日边界；报告日缓存
不因范围/口径变化串包。全部经真实 manager/资格层，替换外部传输并计数。
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from tests.test_choice_integration import choice_transport  # noqa: F401 - fixture 注入


class DataFetcherManagerStub:
    """B2 反例夹具：阻止复用失败后的真实补查路径（断言命中既有有界入口即可）。"""
    def get_capital_flow_context(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise AssertionError("复用被拒绝后应走既有有界补查入口（测试中已替换）")


@pytest.fixture()
def stage0_env(choice_transport, monkeypatch: pytest.MonkeyPatch, tmp_path):
    """真实 manager + 真实 _safe_fundamental，观察行业榜与隐式股东出站。

    计数面：sector 榜专用入口（adapter.get_capital_flow）、iFinD 股东
    （fetcher.shareholders，R9 隐式路径）、Choice 原生 CSD。公共 earnings 等保留
    能力不在此断言零。
    """
    from data_provider.base import DataFetcherManager
    from data_provider.choice_adapter import ChoiceAdapter
    from src.agent.tools import data_tools

    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", DataFetcherManager())
    calls: Dict[str, int] = {"sector_capital": 0, "ifind_shareholders": 0, "csd": 0}
    manager = data_tools._get_fetcher_manager()

    original_sector = manager._fundamental_adapter.get_capital_flow

    def counting_sector(*args: Any, **kwargs: Any) -> Any:
        calls["sector_capital"] += 1
        return original_sector(*args, **kwargs)

    monkeypatch.setattr(manager._fundamental_adapter, "get_capital_flow", counting_sector)

    from data_provider.ifind_fundamental_adapter import IfindFetcher

    original_shareholders = IfindFetcher.shareholders

    def counting_shareholders(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls["ifind_shareholders"] += 1
        return original_shareholders(self, *args, **kwargs)

    monkeypatch.setattr(IfindFetcher, "shareholders", counting_shareholders)

    native = ChoiceAdapter._native_request

    def counting_native(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
        if method == "csd":
            calls["csd"] += 1
        return native(method, args, options, deadline)

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(counting_native))
    return {"calls": calls, "manager": manager}


def test_cache_version_bumped_to_15_and_ownership_uncacheable() -> None:
    from src.deep_research_dims.dim_cache import (
        CACHEABLE_DIMS,
        FUNDAMENTAL_MAPPING_VERSION,
        SCHEMA_VERSION,
    )

    assert SCHEMA_VERSION == 15
    assert FUNDAMENTAL_MAPPING_VERSION == 20
    assert "ownership" not in CACHEABLE_DIMS


def test_stage_zero_capital_block_has_no_sector_rankings(stage0_env) -> None:
    """阶段0资金块只含个股窗口：行业资金榜零取数、零装配。"""
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    with ResearchRun(60).bind():
        ctx = SharedContext("300260", "样本", "2026-10-28")
        fundamental = _safe_fundamental("300260", ctx)
    block = fundamental.get("capital_flow") or {}
    stock_flow = block.get("data", {}).get("stock_flow") or {}
    assert stock_flow.get("coverage", {}).get("10", {}).get("complete"), stock_flow.get("errors")
    assert "sector_rankings" not in fundamental, "行业资金榜不再是共享上下文输入"
    # 真实报告反例：源块以 {} 空占位存在也算装配——退役字段必须整键缺席。
    assert "sector_rankings" not in (block.get("data") or {}), "板块榜退役后不得再构造空占位键"


def test_stage_zero_sector_aux_outbound_is_zero(stage0_env) -> None:
    """R12：阶段0行业榜与隐式 iFinD 股东出站均为零；个股资金 CSD 正常。"""
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    calls = stage0_env["calls"]
    with ResearchRun(60).bind():
        ctx = SharedContext("300260", "样本", "2026-10-28")
        _safe_fundamental("300260", ctx)
    assert calls["sector_capital"] == 0, "行业资金榜退役：阶段0零 sector 专用出站"
    assert calls["ifind_shareholders"] == 0, "机构/股东侧退役：阶段0零 iFinD 股东出站"
    assert calls["csd"] >= 1, "个股资金路径正常工作"


def test_report_day_cache_key_scope_changes_with_scoring_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """评分口径变化后报告日缓存不串包（v2.2 进 key）。

    B3：等价全量请求（None/空/旧19/显式18）规范化为同一 full 指纹（不生成重复
    付费报告）；真子集与仅退役请求不折叠进 full。
    """
    import pytest as _pytest

    from src.schemas.deep_research_dims import (
        ACTIVE_DIM_IDS,
        DIM_IDS,
        RetiredDimRequestError,
        resolve_dim_selection,
    )
    from src.scoring.weights_v2 import SCORING_VERSION_V2
    from src.services.deep_research_service import _date_key

    assert SCORING_VERSION_V2 in _date_key("600519", None)
    # B3：四种等价全量身份一致
    keys = {
        _date_key("600519", None),
        _date_key("600519", []),
        _date_key("600519", list(DIM_IDS)),          # 旧 19（入口解析剔除后传 full 亦同）
        _date_key("600519", sorted(ACTIVE_DIM_IDS)),  # 显式 18
    }
    assert len(keys) == 1, keys
    # 真子集不折叠进 full
    assert _date_key("600519", ["capital"]) != _date_key("600519", None)
    # 仅退役在解析层拒绝（不存在折叠身份）
    with _pytest.raises(RetiredDimRequestError):
        resolve_dim_selection(["ownership"])


def test_day_cache_rejects_cross_close_stale_capital(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """B2 反例：10/28 14:50 生成（T=10/27，采用 10/26 回退），15:10 后 T=10/28，
    10/26 成为 T-2——日缓存命中必须拒绝越界资金、走正常重新生成。"""
    from datetime import date as _date, datetime as _datetime

    from src.services.deep_research_service import _day_cache_capital_within_boundary
    from src.schemas.deep_research_dims import CapitalDim

    stale = CapitalDim(
        status="ok", flow_summary="回退资金。", flow_score=80.0, score=80.0,
        as_of_date="2026-10-26", expected_date="2026-10-27",
    ).model_dump()
    cached = {"status": "success", "dimensions": {"capital": stale}}
    # 读取时刻：10/28 15:10（T 已前进到 10/28）→ 越界拒绝
    assert _day_cache_capital_within_boundary(cached) is False
    # 保存时的 T-1 回退证据跨到次自然日读取：无法证明当前 T 未前进 → 保守 miss
    assert _day_cache_capital_within_boundary(cached, now=_date(2026, 10, 28)) is False
    # 证据保存当日读取（T_save==今天，T-1 回退合法）→ 采信
    assert _day_cache_capital_within_boundary(cached, now=_date(2026, 10, 27)) is True
    # 无资金维度/降级资金的报告不受该边界约束
    assert _day_cache_capital_within_boundary({"status": "success", "dimensions": {}}) is True
    degraded = dict(stale, status="degraded")
    assert _day_cache_capital_within_boundary({"status": "success", "dimensions": {"capital": degraded}}) is True


def test_stage_zero_capital_reuse_rejects_cross_close_block(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B2：跨收盘旧回退资金不得复活——非法块的拒绝在缓存读取边界（24h 快照）
    完成，阶段0复用看到的是剥离后的缺块 → 走既有有界补查（正常 miss）。"""
    from unittest.mock import patch

    from src.config import Config, get_config
    from src.core.research_run import ResearchRun
    from src.deep_research_dims import dim_cache
    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools

    monkeypatch.setattr(Config, "_instance", Config(data_source_profile="choice", enable_ifind_mcp=False, deep_research_cross_validate=False))
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", DataFetcherManagerStub())
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
    # 冻结谓词时钟到声明的读取日（10/29），跨收盘拒绝不依赖测试执行日期。
    _freeze_capital_clock(monkeypatch, "2026-10-29")
    cfg = get_config()
    key = f"stage0_fund_{cfg.data_source_contract}_v{dim_cache.FUNDAMENTAL_MAPPING_VERSION}_cv0_300260"
    dim_cache.save_snapshot(key, {
        "pe_ttm": 40.0, "pb": 3.0, "roe": 18.0, "gross_margin": 40.0,
        "revenue_growth": 10.0, "net_profit_yoy": 10.0, "market_cap": 10000.0,
        "capital_flow": _stale_cross_close_capital_block(),
    })
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    run = ResearchRun(30)
    ctx = SharedContext("300260", "样本", "2026-10-29T15:10:00")
    with run.bind():
        ctx.fundamental = _safe_fundamental("300260", ctx)
        run.context = ctx
        with patch("src.agent.tools.data_tools._handle_get_capital_flow", return_value={"__real_refetch__": True}):
            material = explore_agents._capital_flow_material("300260")
    assert material == {"__real_refetch__": True}, "跨收盘旧回退不得借快照复用复活（读取边界剥离后正常补查）"


def test_capital_dim_cache_boundary_uses_saved_evidence(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R12：capital 缓存边界用保存证据保守判定（真实 CapitalDim 载荷）。

    - as_of==T_save 且 T_save∈{今天,昨天} → 采信；
    - 保存时 T-1 回退（as_of!=T_save）且 T_save==今天 → 采信；
    - T-1 缓存跨次日读取（10/28 晚 10/27 资金在 10/29 读）→ 保守 miss；
    - 未来/缺证据/乱序 → miss。不新增任何探测请求。
    """
    from datetime import date as _date, datetime as _datetime, timedelta

    from src.deep_research_dims import dim_cache
    from src.schemas.deep_research_dims import CapitalDim

    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
    today = _datetime.now().date()

    def _dim(as_of: str, expected: str) -> CapitalDim:
        return CapitalDim(
            status="ok",
            flow_summary="Choice 主力净流入。",
            flow_score=80.0, score=80.0, as_of_date=as_of, expected_date=expected,
        )

    def _load(as_of: str, expected: str) -> Any:
        dim_cache.save_cached_dim("600519", "capital", _dim(as_of, expected).model_dump())
        return dim_cache.load_cached_dim("600519", "capital")

    # 采信 a)：当日采用当日 T
    assert _load(today.isoformat(), today.isoformat()) is not None
    # 采信 a)：昨日 T，今日读取（T_now 最多前进一天）
    assert _load((today - timedelta(days=1)).isoformat(), (today - timedelta(days=1)).isoformat()) is not None
    # 采信 b)：保存时 T-1 回退且 T_save==今天
    assert _load((today - timedelta(days=1)).isoformat(), today.isoformat()) is not None
    # 保守 miss：T-1 缓存跨次日读取（反例 10/27 资金在 10/29 读）——as_of==T_save-1
    # 且 T_save==昨天（≠今天）
    assert _load((today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()) is None
    # 保守 miss：T-1 回退但保存于昨日（T_save==昨天 ≠ 今天）
    assert _load((today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()) is None
    # miss：as_of 晚于 T_save（乱序证据）
    assert _load(today.isoformat(), (today - timedelta(days=1)).isoformat()) is None
    # miss：未来日期
    assert _load("2999-01-01", "2999-01-01") is None
    # miss：缺保存证据（v15 后 capital 缓存必须带两日期）
    assert _load(today.isoformat(), "") is None
    assert _load("", today.isoformat()) is None


def test_stage_zero_context_has_no_sector_rankings_input_in_history_dim_flow(
    stage0_env,
) -> None:
    """F2 板块维度不再消费资金榜：rankings 空且无『排行未取得』缺口提示。"""
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext
    from src.deep_research_dims.sector_dim import build_sector_dim

    with ResearchRun(60).bind():
        ctx = SharedContext("300260", "样本", "2026-10-28")
        from src.deep_research_dims.context import _safe_fundamental

        ctx.fundamental = _safe_fundamental("300260", ctx)
    dim = build_sector_dim(ctx)
    assert dim.rankings == {}
    assert all("排行" not in gap for gap in dim.data_gaps)


def test_stock_info_tool_passes_deep_scope_when_active(stage0_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """B1：模型阶段经 get_stock_info 取数时沿用深研范围（零行业榜/股东出站），
    且能复用阶段0深研 scope 的 manager 缓存；普通消费者保持 full。"""
    from unittest.mock import patch

    from src.agent.tools import data_tools
    from src.core.research_run import ResearchRun, research_dimension
    from src.deep_research_dims.context import SharedContext

    manager = data_tools._get_fetcher_manager()
    sector_before = stage0_env["calls"]["sector_capital"]
    ifind_before = stage0_env["calls"]["ifind_shareholders"]
    # 阶段0深研 scope 先聚合（真实写缓存）
    ctx = SharedContext("300260", "样本", "2026-10-28")
    with ResearchRun(120).bind():
        research_dimension.set("capital")
        from src.deep_research_dims.context import _safe_fundamental

        ctx.fundamental = _safe_fundamental("300260", ctx)
        after_stage0_csd = stage0_env["calls"]["csd"]
        # 模型阶段 get_stock_info：命中深研 scope 缓存，退役操作零新增
        info = data_tools._handle_get_stock_info("300260")
        assert info.get("code") == "300260"
        assert stage0_env["calls"]["sector_capital"] == sector_before
        assert stage0_env["calls"]["ifind_shareholders"] == ifind_before
        assert stage0_env["calls"]["csd"] == after_stage0_csd, "深研 scope 命中缓存，零新增 Choice 出站"
        # 普通 full 消费者（dimension 为空）不命中深研缓存：真实再聚合（其 sector 榜为合法保留路径）
        research_dimension.set("")
        manager.get_fundamental_context("300260", include_sector_flows=True)
        assert stage0_env["calls"]["sector_capital"] >= sector_before


def test_manager_cache_and_single_fetch_keys_isolate_scope(stage0_env) -> None:
    """R9/R9-2：范围隔离用真实 manager 聚合与缓存写入验证（不 mock 风险层）。

    冷/热/跨 scope 语义：深研(False) 冷态真实聚合并真实写入 scope 缓存 → 热态
    命中（Choice 出站不增）；普通消费者(True) 不命中深研缓存 → 真实再聚合；
    全程退役操作（sector 榜/iFinD 股东）零出站。
    """
    from src.agent.tools import data_tools

    manager = data_tools._get_fetcher_manager()
    key_true = manager._get_fundamental_cache_key("300260", None, include_sector_flows=True)
    key_false = manager._get_fundamental_cache_key("300260", None, include_sector_flows=False)
    assert key_true != key_false

    def _csd_total() -> int:
        return stage0_env["calls"]["csd"]

    # 冷态：深研（False）真实聚合（choice_transport 的 SDK 响应驱动）；退役操作零出站
    sector_before = stage0_env["calls"]["sector_capital"]
    ifind_before = stage0_env["calls"]["ifind_shareholders"]
    deep_cold = manager.get_fundamental_context("300260", include_sector_flows=False)
    assert deep_cold.get("market") == "cn"
    assert stage0_env["calls"]["sector_capital"] == sector_before
    assert stage0_env["calls"]["ifind_shareholders"] == ifind_before
    after_deep_cold = _csd_total()
    # 热态：同 scope 再次请求命中 manager 缓存（零新增 Choice 出站）
    deep_warm = manager.get_fundamental_context("300260", include_sector_flows=False)
    assert deep_warm.get("market") == "cn"
    assert _csd_total() == after_deep_cold, "深研热态必须命中真实写入的 scope 缓存"
    assert stage0_env["calls"]["sector_capital"] == sector_before
    assert stage0_env["calls"]["ifind_shareholders"] == ifind_before
    # 跨 scope：普通消费者（True）不采用深研缓存 → 真实再聚合（其行业榜为合法保留路径）
    normal_cold = manager.get_fundamental_context("300260", include_sector_flows=True)
    assert normal_cold.get("market") == "cn"
    assert _csd_total() > after_deep_cold, "普通消费者不得命中深研 scope 缓存"
    normal_warm = manager.get_fundamental_context("300260", include_sector_flows=True)
    assert normal_warm.get("market") == "cn"
    after_normal_warm = _csd_total()
    # 强刷/并行语义由 supplier_need_key 区分（深研与普通不同 run 键，不共享出站）
    del after_normal_warm
    # same-fetch 身份：深研与普通消费者的 run 键不同（并行不共享同一次出站）
    from src.core.research_run import supplier_need_key

    assert supplier_need_key("choice", "c", "fundamental_full", "300260") != supplier_need_key(
        "choice", "c", "fundamental_full:no_sector", "300260"
    )


def test_new_f1_and_context_drop_retired_institution_placeholder(stage0_env) -> None:
    """退役机构占位（R-institution）：新深研阶段0采纳与 F1 financial_safety 均不
    产出 institution_holding_change；渲染不再出现「机构持仓变化：缺失」退役缺口。
    历史保存载荷的标签映射（KEY_ZH_MAP/retained facts）保留兼容，不在本断言面。"""
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext, _safe_fundamental
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim
    from src.deep_research_dims.render import _format_cn

    with ResearchRun(60).bind():
        ctx = SharedContext("300260", "样本", "2026-10-28")
        fundamental = _safe_fundamental("300260", ctx)
    assert "institution_holding_change" not in fundamental, "阶段0不再采纳退役机构标量"
    assert "institution" not in fundamental, "机构块无新报告消费者，不再装配"
    dim = build_fundamental_dim(ctx)
    assert "institution_holding_change" not in dim.financial_safety
    assert "机构持仓" not in _format_cn(dict(dim.financial_safety)), "新 F1 渲染无退役缺失占位"


def _stale_cross_close_capital_block() -> Dict[str, Any]:
    """跨收盘越界证据：保存时 T=10/27 的 T-1 回退（10/26），今天读取已越界。"""
    return {
        "status": "ok",
        "data": {"stock_flow": {
            "main_net_inflow": 100.0, "inflow_5d": 200.0, "inflow_10d": 300.0,
            "as_of_date": "2026-10-26", "expected_date": "2026-10-27",
            "field_meta": {"main_inflow": {"rule_eligible": True, "unit": "currency_base", "value": 100.0}},
        }},
    }


def _qualified_main_inflow_record(value: float = 100.0) -> Dict[str, Any]:
    """既有资格构造器生成的完整合格 main_inflow 记录（choice/CNY/currency_base）。

    手写不完整 field_meta 经 adopted_field_record 重投影会被既有资格层拒绝
    （currency_unknown 等）——测试夹具用与真实解析同一构造链。
    """
    from data_provider.cross_source_validator import AnchorReading, adopted_field_record

    reading = AnchorReading(source="choice", value=value, unit="currency_base", currency="CNY")
    return adopted_field_record("main_inflow", reading)


def _qualified_fresh_capital_block(as_of: str, expected: str) -> Dict[str, Any]:
    """日历合格的 fresh 块（完整 1/5/10 窗口 + 真实资格元数据），日期由调用方声明。"""
    return {
        "status": "ok",
        "data": {"stock_flow": {
            "main_net_inflow": 100.0, "inflow_5d": 200.0, "inflow_10d": 300.0,
            "as_of_date": as_of, "expected_date": expected, "currency": "CNY",
            "field_meta": {"main_inflow": _qualified_main_inflow_record()},
        }},
    }


def _freeze_capital_clock(monkeypatch: pytest.MonkeyPatch, today: str) -> None:
    """冻结共享谓词的时钟缝：转发既有 now 参数（不替代判定），测试日期显式化。"""
    from datetime import date as _date

    from src.deep_research_dims import dim_cache

    real_predicate = dim_cache.capital_block_within_date_boundary

    def _frozen(block: Any, *, now: Any = None) -> bool:
        return real_predicate(block, now=_date.fromisoformat(today))

    monkeypatch.setattr(dim_cache, "capital_block_within_date_boundary", _frozen)


def test_safe_fundamental_snapshot_drops_stale_capital_keeps_finance(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B2：24h 快照读取边界同受共享谓词约束——越界资金块剥离、其余财务证据
    保留（零出站早返回不变）；快照内日历合格资金块原样保留。"""
    from src.config import Config, get_config
    from src.core.research_run import ResearchRun
    from src.agent.tools import data_tools
    from src.deep_research_dims import dim_cache
    from src.deep_research_dims.context import SharedContext, _safe_fundamental

    monkeypatch.setattr(Config, "_instance", Config(data_source_profile="choice", enable_ifind_mcp=False, deep_research_cross_validate=False))
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path))
    cfg = get_config()

    finance: Dict[str, Any] = {
        "pe_ttm": 40.0, "pb": 3.0, "roe": 18.0, "gross_margin": 40.0,
        "revenue_growth": 10.0, "net_profit_yoy": 10.0, "market_cap": 10000.0,
    }

    def _seed(code: str, block: Dict[str, Any]) -> str:
        key = f"stage0_fund_{cfg.data_source_contract}_v{dim_cache.FUNDAMENTAL_MAPPING_VERSION}_cv0_{code}"
        dim_cache.save_snapshot(key, {**finance, "capital_flow": block})
        return key

    _seed("300260", _stale_cross_close_capital_block())
    _seed("300261", _qualified_fresh_capital_block("2026-10-29", "2026-10-29"))
    # 冻结谓词时钟到声明的读取日（10/29），跨收盘/合格判定显式可证。
    _freeze_capital_clock(monkeypatch, "2026-10-29")

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("快照命中（剥离越界资金后）不得重新取数")

    monkeypatch.setattr(data_tools, "_get_fetcher_manager", _boom)
    with ResearchRun(30).bind():
        stale_ctx = SharedContext("300260", "样本", "2026-10-29T15:10:00")
        fund = _safe_fundamental("300260", stale_ctx)
        fresh_ctx = SharedContext("300261", "样本", "2026-10-29T15:10:00")
        fund_fresh = _safe_fundamental("300261", fresh_ctx)
    assert fund["pe_ttm"] == 40.0, "有用财务证据保留（零出站早返回）"
    assert not fund.get("capital_flow"), "跨收盘越界资金块在读取边界剥离"
    assert fund_fresh.get("capital_flow", {}).get("data", {}).get("stock_flow", {}).get("as_of_date") == "2026-10-29", "快照内合格资金块原样保留"


def test_manager_choice_cache_drops_stale_capital_keeps_finance(stage0_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """B2：manager 缓存读取边界只在深研 false-scope 生效——越界资金块剥离、其余
    财务证据保留（零出站）；普通消费者（include_sector_flows=True）按既有契约
    原样返回同一缓存。"""
    import time as _time

    from src.config import get_config

    manager = stage0_env["manager"]
    cfg = get_config()
    _freeze_capital_clock(monkeypatch, "2026-10-29")
    stale = _stale_cross_close_capital_block()
    context: Dict[str, Any] = {
        "market": "cn", "status": "ok",
        "valuation": {"data": {"pe_ratio": 121.6, "pb_ratio": 9.3}},
        "growth": {"data": {"roe": 0.0, "gross_margin": 23.4, "revenue_yoy": 9.7, "net_profit_yoy": -7.5}},
        "capital_flow": stale,
        "errors": [],
    }
    deep_key = f"{cfg.data_source_contract}|{manager._get_fundamental_cache_key('300260', None, include_sector_flows=False)}"
    normal_key = f"{cfg.data_source_contract}|{manager._get_fundamental_cache_key('300261', None, include_sector_flows=True)}"
    with manager._fundamental_cache_lock:
        manager._fundamental_cache[deep_key] = {"ts": _time.time(), "context": dict(context)}
        manager._fundamental_cache[normal_key] = {"ts": _time.time(), "context": dict(context)}
    result = manager.get_fundamental_context("300260", include_sector_flows=False)
    assert result["valuation"]["data"]["pe_ratio"] == 121.6, "财务证据保留"
    assert not result.get("capital_flow"), "越界资金块在深研读取边界剥离"
    assert stage0_env["calls"]["csd"] == 0, "剥离不得触发重新取数"
    # 普通消费者正例：同一越界缓存按既有契约原样返回（不改其他消费者行为）
    normal = manager.get_fundamental_context("300261", include_sector_flows=True)
    assert normal.get("capital_flow") == stale, "普通消费者缓存不受深研边界影响"
    assert stage0_env["calls"]["csd"] == 0


def test_stage_zero_capital_flow_reuses_fresh_calendar_qualified_block(stage0_env) -> None:
    """R-weekend（msg_f22c5ec9357a）：本报告阶段0刚取得的日历合格块（如周末读取
    时 expected=周五）不得被缓存谓词误拒——fresh 块直接复用，零重复出站。"""
    from unittest.mock import patch

    from src.agent.deep_research import explore_agents
    from src.core.research_run import ResearchRun
    from src.deep_research_dims.context import SharedContext

    run = ResearchRun(30)
    ctx = SharedContext("300260", "样本", "2026-10-31T10:00:00")
    ctx.fundamental = {"capital_flow": _qualified_fresh_capital_block("2026-10-30", "2026-10-30")}
    run.context = ctx
    with run.bind():
        with patch(
            "src.agent.tools.data_tools._handle_get_capital_flow",
            side_effect=AssertionError("fresh 合格块复用不得重复补查"),
        ):
            flow = explore_agents._stage_zero_capital_flow("300260", run)
            material = explore_agents._capital_flow_material("300260")
    assert flow is not None and flow.get("main_net_inflow") == 100.0, "fresh 块不再被缓存谓词误拒"
    assert material is not None and material.get("main_net_inflow") == 100.0
