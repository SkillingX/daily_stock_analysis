# -*- coding: utf-8 -*-
"""主力资金流稳定源编排（KISS · fail-open · 不读 config）。

职责：akshare ``push2his.eastmoney.com`` 在代理/限流环境下不可达、导致
``get_capital_flow`` 的 ``inflow_5d``/``inflow_10d``/``main_net_inflow`` 全超时为 None 时，
用 iFinD 多日「主力净流入额」序列作为稳定源算累计，给工具层一个可回填的 dict。

设计（高内聚低耦合）：
- iFinD 多行序列的「格式解析」留在 ``ifind_fundamental_adapter``（它最懂 iFinD 格式）；
  本模块只做「源无关」的累计计算 + 编排（取序列 → 算 today/5d/10d）。
- ``compute_cumulative`` 是纯函数（无 IO），100% 可单测；``get_main_inflow_cumulative``
  编排 iFinD 单例，fail-open（不可用/失败 → ``{}``）。
- 本模块**不读 config / 不感知交叉验证开关**——是否启用回填由调用方（data_tools）按开关决定。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 求和窗口（对齐 get_capital_flow 的 inflow_5d / inflow_10d）
_WINDOW_5D = 5
_WINDOW_10D = 10
# 请求天数（够 10 日求和；交易日少一两个亦无妨，compute_cumulative 不足 n 求全和）
_DEFAULT_FETCH_DAYS = 12


def compute_cumulative(
    series: Optional[List[Tuple[str, float]]], n: int
) -> Optional[float]:
    """对「最新在前」序列取最近 ``n`` 个值求和。

    不足 ``n`` 个则全求和（诚实：有多少算多少）；序列为空/None → None。
    """
    if not series:
        return None
    take = series[: max(1, n)]
    return float(sum(v for _date, v in take))


def get_main_inflow_cumulative(
    code: str, days: int = _DEFAULT_FETCH_DAYS
) -> Dict[str, Any]:
    """用 iFinD 多日序列算主力净流入 today/5d/10d（稳定源，akshare 不可达时兜底）。

    返回 ``{main_net_inflow, inflow_5d, inflow_10d, daily_series, source}``；
    iFinD 未配置 / 抓取失败 / 空序列 → ``{}``（fail-open，不抛异常）。
    """
    from data_provider.ifind_fundamental_adapter import IfindFetcher

    fetcher = IfindFetcher.get_instance()
    if not fetcher.available:
        return {}
    series = fetcher.fetch_main_inflow_series(code, days)
    if not series:
        return {}
    from data_provider.cross_source_validator import AnchorReading, adopted_field_record
    # Legacy series contain scalar values only; they do not prove unit/currency.
    reading = AnchorReading("ifind", series[0][1], observed_at=series[0][0])
    return {
        "main_net_inflow": series[0][1],  # 最新日
        "inflow_5d": compute_cumulative(series, _WINDOW_5D),
        "inflow_10d": compute_cumulative(series, _WINDOW_10D),
        "daily_series": [{"date": d, "value": v} for d, v in series],
        "source": "ifind",
        "field_meta": {"main_inflow": adopted_field_record("main_inflow", reading, selection_reason="fallback")},
    }
