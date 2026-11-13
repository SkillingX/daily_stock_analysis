# -*- coding: utf-8 -*-
"""深研共享实时入口的来源顺序与字段补充契约（issue #93）。

锁定默认 ``realtime_source_priority`` 下 ``DataFetcherManager.get_realtime_quote``
的真实行为（深研阶段 0 与工具 ``get_realtime_quote`` 的共同下游）：

- 非东财（腾讯/新浪）优先，东财（efinance / akshare_em）仅作最后兜底；
- 主源字段齐全时提前返回，不产生东财出站；
- 字段缺失的补充同样非东财优先，只填缺失字段，且每个被采纳字段携带
  自身来源与时间记录；已知过期源（含显式 is_stale）不提供任何字段；
- 补充按默认来源列表有界逐项尝试（每项至多一次出站），只填仍缺且
  合格字段，字段补齐即停，无合格字段返回不阻止后续兜底源；
- 东财之前的源失败/超时按顺序降级，失败证据保留；
- 取消后不再新增出站；过期 provider 时间如实披露不伪装新鲜。
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.core.research_run import ResearchStopped

DEFAULT_PRIORITY = "tencent,akshare_sina,efinance,akshare_em"


def _make_config(priority: str = DEFAULT_PRIORITY) -> SimpleNamespace:
    return SimpleNamespace(
        data_source_profile="legacy",
        enable_realtime_quote=True,
        realtime_source_priority=priority,
        realtime_cache_ttl=600,
    )


def _make_quote(
    source: RealtimeSource,
    *,
    complete: bool = True,
    **overrides,
) -> UnifiedRealtimeQuote:
    """字段齐全的行情；``complete=False`` 时省略补充字段模拟缺口。"""
    fields: dict = {"volume_ratio": 1.1, "turnover_rate": 0.8}
    if complete:
        fields.update(
            {
                "pe_ratio": 25.0,
                "pb_ratio": 8.0,
                "total_mv": 2.0e12,
                "circ_mv": 2.0e12,
                "amplitude": 2.0,
            }
        )
    fields.update(overrides)
    return UnifiedRealtimeQuote(
        code="600519",
        name="贵州茅台",
        source=source,
        price=1688.0,
        change_pct=1.2,
        **fields,
    )


class _EastmoneyFetcher:
    """记录调用的东财假源（efinance token）。"""

    def __init__(self, result=None, error: Exception | None = None):
        self.name = "EfinanceFetcher"
        self.priority = 0
        self._result = result
        self._error = error
        self.calls: list[tuple[tuple, dict]] = []

    def get_realtime_quote(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._error is not None:
            raise self._error
        return self._result


class _MultiSourceFetcher:
    """记录调用的多 token 假源（AkshareFetcher 承载 tencent/sina/em）。"""

    def __init__(self, behaviors: dict[str, object]):
        self.name = "AkshareFetcher"
        self.priority = 0
        self._behaviors = behaviors  # token -> quote | Exception
        self.calls: list[str] = []

    def get_realtime_quote(self, *_args: object, source: str = "em"):
        self.calls.append(source)
        behavior = self._behaviors.get(source)
        if isinstance(behavior, Exception):
            raise behavior
        if behavior is None:
            return None
        return behavior


def test_default_realtime_priority_puts_eastmoney_last(monkeypatch):
    """默认解析结果必须是非东财在前、东财最后（配置契约）。"""
    from src.config import Config

    monkeypatch.delenv("REALTIME_SOURCE_PRIORITY", raising=False)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    resolved = Config._resolve_realtime_source_priority().split(",")
    assert resolved == ["tencent", "akshare_sina", "efinance", "akshare_em"]


@patch("src.config.get_config")
def test_complete_non_eastmoney_quote_makes_zero_eastmoney_calls(mock_get_config):
    """腾讯主源字段齐全时提前返回，东财零调用。"""
    mock_get_config.return_value = _make_config()
    akshare = _MultiSourceFetcher({"tencent": _make_quote(RealtimeSource.TENCENT)})
    efinance = _EastmoneyFetcher()
    manager = DataFetcherManager(fetchers=[akshare, efinance])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is not None
    assert quote.source == RealtimeSource.TENCENT
    assert akshare.calls == ["tencent"]
    assert efinance.calls == []
    assert quote.fetched_at is not None


@patch("src.config.get_config")
def test_second_non_eastmoney_source_succeeds_without_eastmoney_contact(mock_get_config):
    """腾讯失败后新浪补位成功，东财仍零调用；fallback_from 记录首个失败源。"""
    mock_get_config.return_value = _make_config()
    akshare = _MultiSourceFetcher(
        {
            "tencent": RuntimeError("tencent unavailable"),
            "sina": _make_quote(RealtimeSource.AKSHARE_SINA),
        }
    )
    efinance = _EastmoneyFetcher()
    manager = DataFetcherManager(fetchers=[akshare, efinance])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is not None
    assert quote.source == RealtimeSource.AKSHARE_SINA
    assert akshare.calls == ["tencent", "sina"]
    assert efinance.calls == []
    assert quote.fallback_from == "tencent"


@patch("src.config.get_config")
def test_timeout_falls_through_to_next_non_eastmoney_source(mock_get_config):
    """超时按失败降级到下一个非东财源，失败证据保留。"""
    mock_get_config.return_value = _make_config()
    akshare = _MultiSourceFetcher(
        {
            "tencent": TimeoutError("tencent read timeout"),
            "sina": _make_quote(RealtimeSource.AKSHARE_SINA),
        }
    )
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is not None
    assert quote.source == RealtimeSource.AKSHARE_SINA
    assert quote.fallback_from == "tencent"


@patch("src.config.get_config")
def test_eastmoney_reached_only_after_non_eastmoney_sources_fail(mock_get_config):
    """两个非东财源都失败后才轮到 efinance；akshare_em 保持零调用。"""
    mock_get_config.return_value = _make_config()
    akshare = _MultiSourceFetcher(
        {
            "tencent": RuntimeError("tencent unavailable"),
            "sina": RuntimeError("sina unavailable"),
            "em": AssertionError("akshare_em must not be called after efinance success"),
        }
    )
    efinance = _EastmoneyFetcher(result=_make_quote(RealtimeSource.EFINANCE))
    manager = DataFetcherManager(fetchers=[akshare, efinance])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is not None
    assert quote.source == RealtimeSource.EFINANCE
    assert akshare.calls == ["tencent", "sina"]
    assert efinance.calls == [(("600519",), {})]
    assert quote.fallback_from == "tencent"


@patch("src.config.get_config")
def test_missing_field_supplement_prefers_non_eastmoney_source(mock_get_config):
    """主源缺字段时补充同样非东财优先：新浪补齐后东财零调用，且只填缺失字段。"""
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=None)
    supplement = _make_quote(RealtimeSource.AKSHARE_SINA, volume_ratio=1.7)
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": supplement})
    efinance = _EastmoneyFetcher()
    manager = DataFetcherManager(fetchers=[akshare, efinance])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is primary
    assert quote.source == RealtimeSource.TENCENT
    assert quote.volume_ratio == 1.7
    assert quote.pe_ratio == 25.0  # 已合格字段不被补充源覆盖
    assert akshare.calls == ["tencent", "sina"]
    assert efinance.calls == []
    # 锚点字段溯源保留主源身份
    assert quote.field_meta["current_price"]["source"] == "tencent"
    assert quote.field_meta["current_price"]["selection_reason"] == "primary"
    assert quote.field_meta["pe_ratio"]["selection_reason"] == "primary"


@patch("src.config.get_config")
def test_supplemented_field_carries_own_source_and_time(mock_get_config):
    """补充字段必须携带自身来源与时间记录（issue #93 反例 1）。

    volume_ratio/turnover_rate/amplitude 非锚点，此前合并后不生成任何
    字段记录，无法核实各字段自身来源与时间。
    """
    mock_get_config.return_value = _make_config()
    observed = datetime.now(timezone.utc).isoformat()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=None, amplitude=None)
    supplement = _make_quote(
        RealtimeSource.AKSHARE_SINA,
        volume_ratio=1.7,
        amplitude=3.2,
        provider_timestamp=observed,
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": supplement})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    for field, value in (("volume_ratio", 1.7), ("amplitude", 3.2)):
        record = quote.field_meta.get(field)
        assert record is not None, f"{field} 补充后必须有字段溯源记录"
        assert record["value"] == value
        assert record["source"] == "akshare_sina"
        assert record["observed_at"] == observed
        assert record["selection_reason"] == "quote_supplement"
        assert record["is_stale"] is False
        assert "fetched_at" in record  # 原始 fetcher 结果无 fetched_at，如实为 None


@patch("src.config.get_config")
def test_known_stale_secondary_supplies_no_field_silently(mock_get_config):
    """已知过期的补充源不得提供任何字段（issue #93 反例 2）。

    锚点字段经 rule_eligible 已拒绝过期；volume_ratio 等非锚点补充字段
    此前仅判非 None，过期值会被静默采用。
    """
    mock_get_config.return_value = _make_config()
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=None)
    stale_secondary = _make_quote(
        RealtimeSource.AKSHARE_SINA,
        volume_ratio=1.7,
        provider_timestamp=stale_ts,
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": stale_secondary})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote.volume_ratio is None  # 过期补充值不被采用
    assert quote.field_meta.get("volume_ratio") is None  # 不伪装为有效值
    assert quote.is_stale is None  # 主源自身无时间信息，保持未知而非伪造


@patch("src.config.get_config")
def test_explicit_stale_flag_on_secondary_blocks_supply(mock_get_config):
    """secondary 显式 is_stale=True 时同样拒绝（上游 enrich 后传入的场景）。"""
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT, turnover_rate=None)
    stale_secondary = _make_quote(
        RealtimeSource.AKSHARE_SINA, turnover_rate=0.9, is_stale=True
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": stale_secondary})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote.turnover_rate is None
    assert quote.field_meta.get("turnover_rate") is None


@patch("src.config.get_config")
def test_supplement_cap_skips_futile_second_eastmoney_fetch(mock_get_config):
    """非东财部分补齐后，东财仍按序尝试剩余缺口（复审问题 2）。

    腾讯缺 volume_ratio 与 turnover_rate，新浪仅补到 volume_ratio；
    默认顺序下逐项有限尝试，东财作为最后兜底必须被尝试并补齐换手率，
    不因一次部分合并被跳过。默认列表每项至多一次出站，字段补齐即停。
    """
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=None, turnover_rate=None)
    partial = _make_quote(RealtimeSource.AKSHARE_SINA, turnover_rate=None)
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": partial})
    efinance = _EastmoneyFetcher(result=_make_quote(RealtimeSource.EFINANCE))
    manager = DataFetcherManager(fetchers=[akshare, efinance])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote.volume_ratio == 1.1  # 新浪补充
    assert quote.turnover_rate == 0.8  # 东财兜底补齐剩余缺口
    assert akshare.calls == ["tencent", "sina"]
    assert len(efinance.calls) == 1  # 默认列表每项至多一次出站
    # 字段级来源与实际采纳一致
    assert quote.field_meta["volume_ratio"]["source"] == "akshare_sina"
    assert quote.field_meta["turnover_rate"]["source"] == "efinance"
    assert quote.field_meta["turnover_rate"]["selection_reason"] == "quote_supplement"


@patch("src.config.get_config")
def test_fundamental_valuation_block_keeps_financial_anchor_records_only(mock_get_config):
    """基本面估值块只承载金融锚点记录（复审问题 1）。

    quote_field_records 的量比/换手/振幅记录经 _build_fundamental_block
    按 pe/pb/市值语义重建会被误标 unit_incompatible/rule_eligible=False，
    污染 valuation.field_meta 与下游 financial_quality；非锚点不得被
    伪造成金融合格记录。
    """
    mock_get_config.return_value = SimpleNamespace(
        data_source_profile="legacy",
        enable_realtime_quote=True,
        enable_fundamental_pipeline=True,
        realtime_source_priority=DEFAULT_PRIORITY,
        realtime_cache_ttl=600,
        fundamental_stage_timeout_seconds=2.0,
        fundamental_fetch_timeout_seconds=2.0,
        fundamental_cache_ttl_seconds=0,
        fundamental_cache_max_entries=8,
        fundamental_retry_max=1,
    )
    observed = datetime.now(timezone.utc).isoformat()
    akshare = _MultiSourceFetcher(
        {"tencent": _make_quote(RealtimeSource.TENCENT, provider_timestamp=observed)}
    )
    manager = DataFetcherManager(fetchers=[akshare])
    manager._fundamental_adapter = SimpleNamespace(
        get_financial_indicators=lambda code: None,
        get_fundamental_bundle=lambda code, financials=None, include_institution=False: None,
    )
    manager._tushare_ifind_adapter = SimpleNamespace(available=False)
    manager._mx_source = SimpleNamespace(available=False)

    context = manager.get_fundamental_context(
        "600519", budget_seconds=2.0, include_sector_flows=False
    )
    valuation_meta = (context.get("valuation") or {}).get("field_meta") or {}
    anchor_keys = {"current_price", "pe_ratio", "pb_ratio", "total_mv", "circ_mv"}
    assert set(valuation_meta) <= anchor_keys, valuation_meta.keys()
    assert valuation_meta["pe_ratio"]["source"] == "tencent"
    assert valuation_meta["pe_ratio"]["rule_eligible"] is not None
    for field in ("volume_ratio", "turnover_rate", "amplitude"):
        assert field not in valuation_meta

    # 下游 financial_quality 聚合（analysis_context_builder）不再被污染
    from src.services.analysis_context_builder import _build_fundamentals_block

    block = _build_fundamentals_block(SimpleNamespace(fundamental_context=context))
    quality = (block.metadata or {}).get("financial_quality") or {}
    assert set(quality) <= anchor_keys
    for field in ("volume_ratio", "turnover_rate", "amplitude"):
        assert field not in quality


@patch("src.config.get_config")
def test_failed_first_supplement_still_reaches_next_source(mock_get_config):
    """首个补充源失败时上限不计数，后续源仍可补充（不收紧既有可用性）。"""
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=None)
    akshare = _MultiSourceFetcher(
        {"tencent": primary, "sina": RuntimeError("sina unavailable")}
    )
    efinance = _EastmoneyFetcher(result=_make_quote(RealtimeSource.EFINANCE))
    manager = DataFetcherManager(fetchers=[akshare, efinance])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote.source == RealtimeSource.TENCENT
    assert quote.volume_ratio == 1.1  # efinance 补齐缺失字段
    assert quote.fallback_from is None  # 主源未降级，补充不标记 fallback
    assert akshare.calls == ["tencent", "sina"]
    assert len(efinance.calls) == 1


@patch("src.config.get_config")
def test_enrichment_keeps_supplement_record_semantics(mock_get_config):
    """enrich 对补充记录只更新 is_stale，不套用锚点资格语义重写记录。"""
    mock_get_config.return_value = _make_config()
    observed = datetime.now(timezone.utc).isoformat()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=None)
    supplement = _make_quote(
        RealtimeSource.AKSHARE_SINA,
        volume_ratio=1.7,
        provider_timestamp=observed,
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": supplement})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    record = quote.field_meta["volume_ratio"]
    assert record["source"] == "akshare_sina"
    assert record["selection_reason"] == "quote_supplement"
    assert record["unit"] == "multiple"
    assert record["is_stale"] is False
    assert "rule_eligible" not in record  # 未被锚点资格投影重写


@patch("src.config.get_config")
def test_primary_complete_quote_carries_per_field_source_and_time(mock_get_config):
    """主源完整行情的量比/换手/振幅必须携带自身来源与时间（复审反例 1）。

    此前 quote_field_records 仅覆盖锚点与被补充字段；主源自身携带的
    补充字段无逐字段记录，真实工具/报告无法核实其来源与时间。
    """
    mock_get_config.return_value = _make_config()
    observed = datetime.now(timezone.utc).isoformat()
    akshare = _MultiSourceFetcher(
        {"tencent": _make_quote(RealtimeSource.TENCENT, provider_timestamp=observed)}
    )
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    expectations = {
        "volume_ratio": (1.1, "multiple"),
        "turnover_rate": (0.8, "percentage_point"),
        "amplitude": (2.0, "percentage_point"),
    }
    for field, (value, unit) in expectations.items():
        record = quote.field_meta.get(field)
        assert record is not None, f"{field} 缺少逐字段溯源记录"
        assert record["value"] == value
        assert record["source"] == "tencent"
        assert record["observed_at"] == observed
        assert record["selection_reason"] == "primary"
        assert record["is_stale"] is False
        assert record["unit"] == unit


@patch("src.config.get_config")
def test_primary_time_unknown_fields_disclose_unknown(mock_get_config):
    """数据时间未知（无 provider_timestamp）时记录如实披露，不伪造时间。"""
    mock_get_config.return_value = _make_config()
    akshare = _MultiSourceFetcher({"tencent": _make_quote(RealtimeSource.TENCENT)})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    for field in ("volume_ratio", "turnover_rate", "amplitude"):
        record = quote.field_meta.get(field)
        assert record is not None, f"{field} 缺少逐字段溯源记录"
        assert record["source"] == "tencent"
        assert record["observed_at"] is None
        assert record["is_stale"] is None
        assert record["selection_reason"] == "primary"


@patch("src.config.get_config")
def test_merge_provenance_not_overwritten_by_primary_projection(mock_get_config):
    """混合补充后，补充字段的采纳时记录不得被主源投影覆盖。

    quote_field_records 投影主源字段时，已有 quote_supplement 记录的
    字段必须保持补充源身份，否则来源与实际采纳结果不一致。
    """
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=None)
    supplement = _make_quote(
        RealtimeSource.AKSHARE_SINA,
        volume_ratio=1.7,
        provider_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": supplement})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    record = quote.field_meta["volume_ratio"]
    assert record["source"] == "akshare_sina"  # 保留补充源身份
    assert record["selection_reason"] == "quote_supplement"
    # 主源自身字段仍获得主源记录
    assert quote.field_meta["turnover_rate"]["source"] == "tencent"
    assert quote.field_meta["turnover_rate"]["selection_reason"] == "primary"


@patch("src.config.get_config")
def test_unadoptable_supplement_does_not_block_eastmoney_fallback(mock_get_config):
    """复审反例 2：无可采纳字段的返回不得当作成功补充阻止东财兜底。

    主源缺 turnover_rate；新浪返回基本行情但该字段缺失（merged 为空）；
    此前 supplement_attempts 在合并前无条件自增，下一轮在东财兜底前 break。
    """
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT, turnover_rate=None)
    bare_sina = _make_quote(
        RealtimeSource.AKSHARE_SINA, volume_ratio=None, turnover_rate=None
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": bare_sina})
    efinance = _EastmoneyFetcher(result=_make_quote(RealtimeSource.EFINANCE))
    manager = DataFetcherManager(fetchers=[akshare, efinance])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote.source == RealtimeSource.TENCENT
    assert quote.turnover_rate == 0.8  # 东财兜底补齐必要缺口
    assert akshare.calls == ["tencent", "sina"]
    assert len(efinance.calls) == 1
    assert quote.field_meta["turnover_rate"]["selection_reason"] == "quote_supplement"
    assert quote.field_meta["turnover_rate"]["source"] == "efinance"


@patch("src.config.get_config")
def test_stale_supplement_source_still_allows_eastmoney_fallback(mock_get_config):
    """过期补充源被拒后同样继续东财兜底（与缺失字段同语义）。"""
    mock_get_config.return_value = _make_config()
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    primary = _make_quote(RealtimeSource.TENCENT, turnover_rate=None)
    stale_sina = _make_quote(
        RealtimeSource.AKSHARE_SINA,
        turnover_rate=0.9,
        provider_timestamp=stale_ts,
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": stale_sina})
    efinance = _EastmoneyFetcher(result=_make_quote(RealtimeSource.EFINANCE))
    manager = DataFetcherManager(fetchers=[akshare, efinance])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote.turnover_rate == 0.8  # efinance 合格值被采纳
    assert len(efinance.calls) == 1
    assert quote.field_meta["turnover_rate"]["source"] == "efinance"


@patch("src.config.get_config")
def test_future_timestamped_secondary_supplies_nothing(mock_get_config):
    """时间未来值是不合格输入：来源被拒，字段不留记录（复审要求核查）。"""
    mock_get_config.return_value = _make_config()
    future_ts = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=None)
    future_sina = _make_quote(
        RealtimeSource.AKSHARE_SINA,
        volume_ratio=1.7,
        provider_timestamp=future_ts,
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": future_sina})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote.volume_ratio is None
    assert quote.field_meta.get("volume_ratio") is None


@patch("src.config.get_config")
def test_non_finite_supplement_values_not_adopted(mock_get_config):
    """非有限值（NaN/Inf）不得被采纳或展示为有效字段。"""
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=None)
    bad_sina = _make_quote(RealtimeSource.AKSHARE_SINA, volume_ratio=float("nan"))
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": bad_sina})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote.volume_ratio is None
    assert quote.field_meta.get("volume_ratio") is None


@patch("src.config.get_config")
def test_cancellation_after_primary_stops_new_outbound(mock_get_config):
    """已获主源后取消：不再联系后续源，返回已得行情。"""
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT, complete=False, volume_ratio=None)
    akshare = _MultiSourceFetcher(
        {"tencent": primary, "sina": ResearchStopped("run cancelled")}
    )
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is primary
    assert akshare.calls == ["tencent", "sina"]


@patch("src.config.get_config")
def test_cancellation_before_primary_propagates(mock_get_config):
    """未获任何主源时取消向上传播，不静默降级。"""
    mock_get_config.return_value = _make_config()
    akshare = _MultiSourceFetcher({"tencent": ResearchStopped("run cancelled")})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"), pytest.raises(ResearchStopped):
        manager.get_realtime_quote("600519")
    assert akshare.calls == ["tencent"]


@patch("src.config.get_config")
def test_stale_provider_timestamp_is_disclosed_not_faked_fresh(mock_get_config):
    """过期 provider 时间：is_stale 如实标记，逐字段记录过期，不伪装新鲜。"""
    mock_get_config.return_value = _make_config()
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    akshare = _MultiSourceFetcher(
        {
            "tencent": _make_quote(
                RealtimeSource.TENCENT, provider_timestamp=stale_ts
            )
        }
    )
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("src.services.run_diagnostics.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is not None
    assert quote.is_stale is True
    assert quote.stale_seconds is not None and quote.stale_seconds >= 7200 - 60
    assert quote.provider_timestamp == stale_ts
    records = dict(quote.field_meta)
    assert records["current_price"]["is_stale"] is True
    assert records["current_price"]["observed_at"] == stale_ts
