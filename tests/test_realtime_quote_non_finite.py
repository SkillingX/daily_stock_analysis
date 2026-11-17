# -*- coding: utf-8 -*-
"""统一实时行情的非有限数值边界（PR #95 审查修复）。

以真实 ``DataFetcherManager.get_realtime_quote`` 与深研工具入口
``_handle_get_realtime_quote`` 验证：manager 在采纳判断（
``has_basic_data``）之前把行情对象上暴露的 NaN/±inf 标量按缺失处理——

- 主源价格非有限 → 该源不可用，按既有来源顺序降级（fallback_from 保留）；
- 主源补充字段非有限 → 视为缺失，合格备源按默认顺序补齐并携带自身
  来源记录；无合格备源保持缺失、不留记录；
- 合法 0 / 有限负值 / Decimal / NumPy 数值不被误删；工具响应可严格
  JSON 序列化；港美股共享入口（``_try_fetcher_quote``）同语义。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import (
    RealtimeSource,
    UnifiedRealtimeQuote,
    is_non_finite_number,
    sanitize_non_finite_quote_scalars,
)


DEFAULT_PRIORITY = "tencent,akshare_sina,efinance,akshare_em"


def _make_config(**extra) -> SimpleNamespace:
    return SimpleNamespace(
        data_source_profile="legacy",
        enable_realtime_quote=True,
        realtime_source_priority=DEFAULT_PRIORITY,
        realtime_cache_ttl=600,
        **extra,
    )


def _make_quote(
    source: RealtimeSource,
    *,
    price: float = 1688.0,
    **overrides,
) -> UnifiedRealtimeQuote:
    """字段齐全的行情；overrides 覆盖任意标量以注入异常值。"""
    fields: dict = {
        "volume_ratio": 1.1,
        "turnover_rate": 0.8,
        "pe_ratio": 25.0,
        "pb_ratio": 8.0,
        "total_mv": 2.0e12,
        "circ_mv": 2.0e12,
        "amplitude": 2.0,
        "change_pct": 1.2,
    }
    fields.update(overrides)
    return UnifiedRealtimeQuote(
        code="600519", name="贵州茅台", source=source, price=price, **fields
    )


class _MultiSourceFetcher:
    """记录调用的多 token 假源（AkshareFetcher 承载 tencent/sina/em）。"""

    def __init__(self, behaviors: dict[str, object]):
        self.name = "AkshareFetcher"
        self.priority = 0
        self._behaviors = behaviors
        self.calls: list[str] = []

    def get_realtime_quote(self, *_args: object, source: str = "em"):
        self.calls.append(source)
        behavior = self._behaviors.get(source)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _NamedFetcher:
    """单行为假源（yfinance / longbridge 等命名 token）。"""

    def __init__(self, name: str, result=None):
        self.name = name
        self.priority = 0
        self._result = result
        self.calls: list[tuple[tuple, dict]] = []

    def get_realtime_quote(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._result


@pytest.mark.parametrize("bad_price", [float("inf"), float("-inf"), float("nan")])
@patch("src.config.get_config")
def test_primary_price_non_finite_falls_back_to_next_source(mock_get_config, bad_price):
    """主源价格 NaN/±inf 视为该源不可用，按既有顺序降级而非采纳无效价。"""
    mock_get_config.return_value = _make_config()
    akshare = _MultiSourceFetcher(
        {
            "tencent": _make_quote(RealtimeSource.TENCENT, price=bad_price),
            "sina": _make_quote(RealtimeSource.AKSHARE_SINA),
        }
    )
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("data_provider.base.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is not None
    assert quote.source == RealtimeSource.AKSHARE_SINA
    assert quote.price == 1688.0
    assert quote.fallback_from == "tencent"
    assert akshare.calls == ["tencent", "sina"]
    assert math.isfinite(quote.price)


@patch("src.config.get_config")
def test_primary_non_finite_fields_purged_then_backup_fills(mock_get_config):
    """主源多字段非有限 → 采纳副本按缺失处理，fetcher 原引用不被改动。

    量比(+inf)/振幅(-inf)/涨跌幅(NaN) 清理发生在副本上，manager 采纳副本
    并由新浪补齐量比与换手率；振幅新浪也没有则保持缺失、无记录；fetcher
    持有的原对象字段保持原值（可复用引用不受 manager 采纳影响）。
    """
    mock_get_config.return_value = _make_config()
    primary = _make_quote(
        RealtimeSource.TENCENT,
        volume_ratio=float("inf"),
        amplitude=float("-inf"),
        change_pct=float("nan"),
    )
    backup = _make_quote(
        RealtimeSource.AKSHARE_SINA,
        volume_ratio=1.7,
        amplitude=None,
        provider_timestamp=None,
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": backup})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("data_provider.base.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is not primary  # 清理产生副本，fetcher 原引用不被采纳路径复用
    # fetcher 持有的原对象保持原值，未被 manager 原地清理
    assert primary.volume_ratio == float("inf")
    assert primary.amplitude == float("-inf")
    assert math.isnan(primary.change_pct)
    # 副本上的非法标量按缺失处理并由新浪补齐
    assert quote.volume_ratio == 1.7  # 新浪合格值补齐
    assert quote.amplitude is None
    assert quote.change_pct is None
    assert quote.price == 1688.0  # 有效字段不受清理影响
    assert quote.pe_ratio == 25.0  # 已合格字段不被补充源覆盖
    # 补齐字段携带备源身份；被清理且未补齐的字段不留旧记录
    assert quote.field_meta["volume_ratio"]["source"] == "akshare_sina"
    assert quote.field_meta["volume_ratio"]["selection_reason"] == "quote_supplement"
    assert quote.field_meta.get("amplitude") is None
    assert quote.field_meta.get("change_pct") is None
    # amplitude 缺口未被补齐 → 默认列表继续有界尝试（含 em），逐项至多一次
    assert akshare.calls == ["tencent", "sina", "em"]


@patch("src.config.get_config")
def test_valid_primary_object_returned_as_is(mock_get_config):
    """无非法字段的正常行情原样返回同一对象（兼容既有行为，不复制）。"""
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT)
    akshare = _MultiSourceFetcher({"tencent": primary})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("data_provider.base.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is primary


@patch("src.config.get_config")
def test_non_finite_field_without_qualified_backup_stays_missing(mock_get_config):
    """无合格备源时被清理字段保持缺失、不伪装有效值。"""
    mock_get_config.return_value = _make_config()
    primary = _make_quote(RealtimeSource.TENCENT, volume_ratio=float("inf"))
    bare_sina = _make_quote(
        RealtimeSource.AKSHARE_SINA, volume_ratio=float("nan")
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": bare_sina})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("data_provider.base.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote.volume_ratio is None
    assert quote.field_meta.get("volume_ratio") is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (float("nan"), True),
        (float("inf"), True),
        (float("-inf"), True),
        (0.0, False),
        (-2.5, False),
        (Decimal("NaN"), True),
        (Decimal("sNaN"), True),
        (Decimal("Infinity"), True),
        (Decimal("-Infinity"), True),
        (Decimal("1e1000"), False),  # 超出 float 范围的有限 Decimal 不误删
        (Decimal("12.3"), False),
        (10**400, False),  # 恒有限的大整数，不抛 OverflowError
        (np.float64("nan"), True),
        (np.float64("inf"), True),
        (np.float64(9.9), False),
        (np.int64(1000), False),
        ("1.23", False),  # 非数值字符串不转换、不判定
        ("inf", False),
        ("nan", False),
        ("1e999", False),
        (b"inf", False),
        (True, False),
        (None, False),
    ],
)
def test_is_non_finite_number_classification(value, expected):
    """数值有效性判定：确认为非有限才视为缺失，转换溢出不拒绝数据。"""
    assert is_non_finite_number(value) is expected


def test_longdouble_finite_value_beyond_float64_is_platform_deterministic():
    """longdouble 有限值按平台真实语义判定，不经 float64 饱和转换误删。

    Linux/amd64 上 np.longdouble("1e400") 是有限值（表示范围比 float64
    大）必须保留；macOS arm64 上 longdouble 即 float64，转换结果就是真实
    inf，按非有限处理。两个分支都是平台确定性语义，不机械假设。
    """
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", RuntimeWarning)
        ld = np.longdouble("1e400")
    if np.isfinite(ld):
        # longdouble 表示范围超出 float64 的平台：有限大值不被误删
        assert is_non_finite_number(ld) is False
    else:
        assert float(ld) == float("inf")
        assert is_non_finite_number(ld) is True


@pytest.mark.parametrize(
    "value",
    [0.0, -2.5, Decimal("12.3"), Decimal("1e1000"), 10**400, np.float64(9.9), np.int64(1000)],
)
def test_valid_scalars_survive_sanitization(value):
    """合法 0 / 有限负值 / Decimal / 大整数 / NumPy 数值不被误删。"""
    quote = UnifiedRealtimeQuote("600519", price=10.0, volume_ratio=value)
    cleaned = sanitize_non_finite_quote_scalars(quote)
    assert cleaned is quote  # 无非法字段原样返回
    assert cleaned.volume_ratio == value


def test_sanitizer_drops_stale_meta_for_invalid_scalars():
    """非法标量的旧 field_meta 记录随副本清理，原对象 metadata 不被改。

    预置 current_price 别名键（selection_reason=primary）与
    quote_supplement 记录：标量置缺失时对应旧记录一并移除，避免旧值
    挂到新状态上；保留记录做浅复制（下游 enrich 会原地改写
    record["is_stale"]），fetcher 原对象的嵌套 metadata 不被传染。
    """
    quote = UnifiedRealtimeQuote(
        "600519",
        price=float("inf"),
        volume_ratio=float("inf"),
        pe_ratio=25.0,
        field_meta={
            "current_price": {
                "field": "current_price", "value": 999.0,
                "source": "tencent", "selection_reason": "primary",
            },
            "volume_ratio": {
                "field": "volume_ratio", "value": 3.3,
                "source": "efinance", "selection_reason": "quote_supplement",
            },
            "pe_ratio": {
                "field": "pe_ratio", "value": 25.0,
                "source": "tencent", "selection_reason": "primary",
            },
        },
    )
    cleaned = sanitize_non_finite_quote_scalars(quote)
    assert cleaned is not quote
    assert set(cleaned.field_meta) == {"pe_ratio"}  # 无效标量的旧记录被移除
    assert set(quote.field_meta) == {"current_price", "volume_ratio", "pe_ratio"}
    assert quote.price == float("inf")  # 原对象不被改动
    assert cleaned.field_meta["pe_ratio"] is not quote.field_meta["pe_ratio"]


@patch("src.config.get_config")
def test_enrich_does_not_mutate_fetcher_held_nested_meta(mock_get_config):
    """真实 manager→enrich 反例：副本的嵌套 record 改写不传染原对象。

    主源 price/turnover_rate 有效、volume_ratio=inf；预置 turnover_rate
    旧 quote_supplement 记录（observed_at=2 小时前、is_stale=False）。
    sanitize 副本被采纳后 enrich 原地更新副本记录的 is_stale=True；
    fetcher 持有的原对象外层与嵌套 metadata 都保持原值。
    """
    mock_get_config.return_value = _make_config()
    observed = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    stale_record = {
        "field": "turnover_rate", "value": 0.8, "source": "efinance",
        "unit": "percentage_point", "observed_at": observed,
        "fetched_at": None, "is_stale": False,
        "selection_reason": "quote_supplement",
    }
    primary = UnifiedRealtimeQuote(
        code="600519",
        name="贵州茅台",
        source=RealtimeSource.TENCENT,
        provider_timestamp=datetime.now(timezone.utc).isoformat(),
        price=1688.0,
        change_pct=1.2,
        volume_ratio=float("inf"),
        turnover_rate=0.8,
        pe_ratio=25.0,
        pb_ratio=8.0,
        total_mv=2.0e12,
        circ_mv=2.0e12,
        amplitude=2.0,
        field_meta={"turnover_rate": stale_record},
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": None, "em": None})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("data_provider.base.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is not primary
    assert quote.volume_ratio is None  # 非法标量在副本上按缺失处理
    # enrich 在副本上如实改写：旧 observed_at 超过 TTL → is_stale=True
    assert quote.field_meta["turnover_rate"]["is_stale"] is True
    # fetcher 原对象的外层与嵌套 metadata 均不被传染
    assert primary.field_meta["turnover_rate"] is stale_record
    assert primary.field_meta["turnover_rate"]["is_stale"] is False
    assert primary.field_meta["turnover_rate"] is not quote.field_meta["turnover_rate"]
    assert primary.volume_ratio == float("inf")


@patch("src.config.get_config")
def test_manager_replaces_stale_supplement_meta_after_purge(mock_get_config):
    """真实入口反例：主源携带旧 quote_supplement 记录 + 非法标量。

    预置 volume_ratio=inf 与旧 efinance 补充记录（value=3.3），合格新浪
    补齐后记录必须是新浪的新值，旧记录不得残留；current_price 元数据与
    有效价格保持主源身份一致。
    """
    mock_get_config.return_value = _make_config()
    primary = _make_quote(
        RealtimeSource.TENCENT,
        volume_ratio=float("inf"),
        field_meta={
            "current_price": {
                "field": "current_price", "value": 1688.0,
                "source": "tencent", "selection_reason": "primary",
            },
            "volume_ratio": {
                "field": "volume_ratio", "value": 3.3,
                "source": "efinance", "selection_reason": "quote_supplement",
            },
        },
    )
    backup = _make_quote(RealtimeSource.AKSHARE_SINA, volume_ratio=1.7)
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": backup})
    manager = DataFetcherManager(fetchers=[akshare])

    with patch("data_provider.base.record_provider_run"):
        quote = manager.get_realtime_quote("600519")

    assert quote is not primary
    record = quote.field_meta["volume_ratio"]
    assert record["source"] == "akshare_sina"
    assert record["value"] == 1.7
    assert record["selection_reason"] == "quote_supplement"
    # current_price 元数据与有效价格保持主源身份一致
    assert quote.field_meta["current_price"]["source"] == "tencent"
    assert quote.field_meta["current_price"]["value"] == 1688.0
    assert primary.field_meta["volume_ratio"]["value"] == 3.3  # 原对象记录未被动


@patch("src.config.get_config")
def test_tool_response_strict_json_without_qualified_backup(mock_get_config):
    """深研工具入口：注入异常值的行情响应可严格 JSON 序列化。

    真实 ``_handle_get_realtime_quote`` 消费真实 manager 结果：无效标量
    不出现在响应、无对应 field_meta 记录，``allow_nan=False`` 不抛错。
    """
    mock_get_config.return_value = _make_config(
        enabled_markets=("cn", "hk", "us"),
    )
    primary = _make_quote(
        RealtimeSource.TENCENT,
        volume_ratio=float("inf"),
        change_pct=float("nan"),
        amplitude=float("-inf"),
    )
    akshare = _MultiSourceFetcher({"tencent": primary, "sina": None})
    manager = DataFetcherManager(fetchers=[akshare])

    from src.agent.tools import data_tools
    from src.agent.tools import cross_validation_helpers

    with patch("data_provider.base.record_provider_run"), patch.object(
        cross_validation_helpers, "_get_validator", lambda: None
    ), patch.object(data_tools, "_fetcher_manager_singleton", manager):
        response = data_tools._handle_get_realtime_quote("600519")

    assert "error" not in response
    strict = json.dumps(response, allow_nan=False)
    assert strict  # 序列化成功即可
    assert response["volume_ratio"] is None
    assert response["change_pct"] is None
    assert response["amplitude"] is None
    assert response["price"] == 1688.0
    assert "volume_ratio" not in response["field_meta"]
    assert "amplitude" not in response["field_meta"]


@pytest.mark.parametrize("market_code,market_tag", [("hk00700", "hk"), ("AAPL", "us")])
@patch("src.config.get_config")
def test_hk_us_shared_entry_purges_primary_and_keeps_order(mock_get_config, market_code, market_tag):
    """港美股共享入口（_try_fetcher_quote）：主源价格异常 → 备源补位。

    未配置长桥键时的既有顺序（港股 Akshare→Longbridge，美股
    Yfinance→Longbridge）；清理发生在 has_basic_data 之前，主源不可用
    语义与 A 股一致。
    """
    mock_get_config.return_value = _make_config(
        enabled_markets=("cn", "hk", "us"),
    )
    bad = UnifiedRealtimeQuote(code=market_code, name="bad", price=float("inf"))
    good = UnifiedRealtimeQuote(
        code=market_code, name="good", price=300.0, volume_ratio=1.2
    )
    if market_tag == "hk":
        primary_fetcher = SimpleNamespace(
            name="AkshareFetcher",
            priority=0,
            get_realtime_quote=lambda *a, **kw: (
                bad if kw.get("source") == "hk" else None
            ),
        )
    else:
        primary_fetcher = _NamedFetcher("YfinanceFetcher", bad)
    secondary_fetcher = _NamedFetcher("LongbridgeFetcher", good)
    manager = DataFetcherManager(fetchers=[primary_fetcher, secondary_fetcher])
    manager._longbridge_preferred = lambda *a, **k: False  # 未配置长桥键

    with patch("data_provider.base.record_provider_run"):
        quote = manager.get_realtime_quote(market_code)

    assert quote is good
    assert quote.price == 300.0
    assert secondary_fetcher.calls  # 备源确实被调用补位


@pytest.mark.parametrize("market_code,primary_name", [("hk00700", "AkshareFetcher"), ("AAPL", "YfinanceFetcher")])
@patch("src.config.get_config")
def test_hk_us_shared_entry_supplements_missing_fields(mock_get_config, market_code, primary_name):
    """港美股共享入口：主源价格正常但补充字段非有限 → 备源按缺口补齐。"""
    mock_get_config.return_value = _make_config(
        enabled_markets=("cn", "hk", "us"),
    )
    primary = UnifiedRealtimeQuote(
        code=market_code,
        name="primary",
        price=310.0,
        volume_ratio=float("inf"),
    )
    if primary_name == "AkshareFetcher":
        primary_fetcher = SimpleNamespace(
            name=primary_name,
            priority=0,
            get_realtime_quote=lambda *a, **kw: (
                primary if kw.get("source") == "hk" else None
            ),
        )
    else:
        primary_fetcher = _NamedFetcher(primary_name, primary)
    backup = UnifiedRealtimeQuote(
        code=market_code,
        name="backup",
        source=RealtimeSource.LONGBRIDGE,
        price=311.0,
        volume_ratio=1.4,
    )
    secondary_fetcher = _NamedFetcher("LongbridgeFetcher", backup)
    manager = DataFetcherManager(fetchers=[primary_fetcher, secondary_fetcher])
    manager._longbridge_preferred = lambda *a, **k: False  # 未配置长桥键

    with patch("data_provider.base.record_provider_run"):
        quote = manager.get_realtime_quote(market_code)

    assert quote is not primary  # 非法标量清理产生副本
    assert primary.volume_ratio == float("inf")  # fetcher 原引用不被改动
    assert quote.price == 310.0  # 主源有效价不被覆盖
    assert quote.volume_ratio == 1.4  # 备源补齐被清理的缺口
    assert quote.field_meta["volume_ratio"]["source"] == "longbridge"
    assert quote.field_meta["volume_ratio"]["selection_reason"] == "quote_supplement"


@pytest.mark.parametrize(
    "age_seconds,expect_filled",
    [(-7200, False), (0, True)],
    ids=["stale_backup_rejected", "fresh_backup_adopted"],
)
@patch("src.config.get_config")
def test_hk_entry_supplement_time_qualification(mock_get_config, age_seconds, expect_filled):
    """港美股共享入口的补充时效：过期备源拒绝，新鲜备源采纳并如实标记。

    与 A 股补充同契约——已知过期（provider_timestamp 超 TTL）的备源不提供
    字段、不留记录；合格备源补充后记录携带 observed_at 且 is_stale=False。
    时间戳在测试执行期生成：parametrize 相对秒数，长时间收集到执行的间隔
    不会把 fresh 分支变陈旧（CI 反例）。
    """
    provider_timestamp = (
        datetime.now(timezone.utc) + timedelta(seconds=age_seconds)
    ).isoformat()
    mock_get_config.return_value = _make_config(enabled_markets=("cn", "hk", "us"))
    primary = UnifiedRealtimeQuote(
        code="hk00700", name="primary", price=310.0, volume_ratio=float("inf")
    )
    hk_primary = SimpleNamespace(
        name="AkshareFetcher",
        priority=0,
        get_realtime_quote=lambda *a, **kw: (
            primary if kw.get("source") == "hk" else None
        ),
    )
    backup = UnifiedRealtimeQuote(
        code="hk00700",
        name="backup",
        source=RealtimeSource.LONGBRIDGE,
        price=311.0,
        volume_ratio=1.4,
        provider_timestamp=provider_timestamp,
    )
    secondary_fetcher = _NamedFetcher("LongbridgeFetcher", backup)
    manager = DataFetcherManager(fetchers=[hk_primary, secondary_fetcher])
    manager._longbridge_preferred = lambda *a, **k: False

    with patch("data_provider.base.record_provider_run"):
        quote = manager.get_realtime_quote("hk00700")

    assert quote is not primary  # 主源非法量比清理产生副本
    assert primary.volume_ratio == float("inf")  # fetcher 原引用不被改动
    if expect_filled:
        record = quote.field_meta["volume_ratio"]
        assert quote.volume_ratio == 1.4
        assert record["source"] == "longbridge"
        assert record["observed_at"] == provider_timestamp
        assert record["is_stale"] is False
    else:
        assert quote.volume_ratio is None  # 过期备源被拒，字段保持缺失
        assert quote.field_meta.get("volume_ratio") is None  # 不伪装有效值
