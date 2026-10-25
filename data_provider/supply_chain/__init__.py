# -*- coding: utf-8 -*-
"""
P2 Supply Chain Data Provider.

Provides supply chain, concept board, institutional holdings, and northbound flow data.
"""

from datetime import date
from typing import Optional

from data_provider.supply_chain.institutional_holdings import (
    InstitutionalHoldingsProvider,
)
from data_provider.supply_chain.northbound_flow import NorthboundFlowProvider
from data_provider.supply_chain.tushare_provider import TushareSupplyChainProvider


def latest_fiscal_year(today: Optional[date] = None) -> int:
    """返回最新已完整披露的 A 股年报年份。

    年报须于次年 4/30 前披露：5 月起可用上一年年报，否则用再前一年。
    """
    today = today or date.today()
    return today.year - 1 if today.month >= 5 else today.year - 2


__all__ = [
    "InstitutionalHoldingsProvider",
    "NorthboundFlowProvider",
    "TushareSupplyChainProvider",
    "latest_fiscal_year",
]
