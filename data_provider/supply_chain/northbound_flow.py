# -*- coding: utf-8 -*-
"""
Northbound Flow Data Provider.

Fetches northbound flow (沪深港通) and margin balance data.
"""

import logging
from types import ModuleType
from typing import Dict, Any, List, Optional, cast
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class NorthboundFlowProvider:
    """
    Provider for northbound flow data.

    Uses akshare to fetch:
    - Northbound (沪深港通北向) holdings and flows
    - Margin balance (融资融券) data
    - Stock connect quota usage
    """

    def __init__(self):
        self._ak: Optional[ModuleType] = None
        self._init_akshare()

    def _init_akshare(self):
        """Lazy initialize akshare"""
        if self._ak is None:
            try:
                import akshare as ak

                self._ak = ak
                logger.info("[NorthboundFlowProvider] akshare initialized")
            except ImportError:
                logger.warning("[NorthboundFlowProvider] akshare not installed")
                self._ak = None

    def get_northbound_flow(
        self,
        symbol: str = "北上",
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """
        Get northbound flow data.

        Args:
            symbol: "北上" for northbound (from HK to CN), "南下" for southbound
            end_date: End date in YYYYMMDD format
            adjust: Price adjustment type

        Returns:
            List of daily flow data
        """
        if self._ak is None:
            self._init_akshare()
            if self._ak is None:
                return self._get_mock_northbound_flow()

        try:
            if end_date is None:
                end_date = datetime.now().strftime("%Y%m%d")

            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

            df = self._ak.stock_connect_hist_em(  # type: ignore[reportAttributeAccessIssue]
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )

            flows = []
            for _, row in df.iterrows():
                flows.append(
                    {
                        "date": str(row.get("日期", "")),
                        "close": float(row.get("收盘", 0) or 0),
                        "change_pct": float(row.get("涨跌幅", 0) or 0),
                        "volume": float(row.get("成交量", 0) or 0),
                        "amount": float(row.get("成交额", 0) or 0),
                        "net_inflow": float(row.get("净流入", 0) or 0),
                    }
                )

            logger.info(
                f"[NorthboundFlowProvider] Fetched {len(flows)} days of northbound data"
            )
            return flows

        except Exception as e:
            logger.error(
                f"[NorthboundFlowProvider] Failed to fetch northbound flow: {e}"
            )
            return self._get_mock_northbound_flow()

    def get_stock_northbound_holdings(self, stock_code: str) -> Dict[str, Any]:
        """
        Get northbound holdings for a specific stock.

        Args:
            stock_code: Stock code (e.g., 600519)

        Returns:
            Dict with holdings data
        """
        if self._ak is None:
            self._init_akshare()
            if self._ak is None:
                return {}

        try:
            df = self._ak.stock_hsgt_north_hold_stock_em(symbol=stock_code)  # type: ignore[reportAttributeAccessIssue]

            holdings: Dict[str, Any] = {
                "stock_code": stock_code,
                "holdings": [],
                "total_holdings": 0,
                "total_market_value": 0,
            }

            for _, row in df.iterrows():
                row_dict: Dict[str, Any] = cast(Dict[str, Any], row.to_dict())
                holdings["holdings"].append(
                    {
                        "date": str(row_dict.get("日期", "")),
                        "hold_ratio": float(row_dict.get("持股量", 0) or 0),
                        "market_value": float(row_dict.get("市值", 0) or 0),
                        "holding_ratio": float(row_dict.get("持股占比", 0) or 0),
                    }
                )

            if holdings["holdings"]:
                latest = cast(Dict[str, Any], holdings["holdings"][0])
                holdings["total_holdings"] = latest.get("hold_ratio", 0)
                holdings["total_market_value"] = latest.get("market_value", 0)

            return holdings

        except Exception as e:
            logger.error(f"[NorthboundFlowProvider] Failed to fetch holdings: {e}")
            return {}

    def get_margin_balance(self, stock_code: str) -> Dict[str, Any]:
        """
        Get margin balance data for a stock.

        Args:
            stock_code: Stock code

        Returns:
            Dict with margin balance data
        """
        if self._ak is None:
            self._init_akshare()
            if self._ak is None:
                return {}

        try:
            df = self._ak.stock_margin_detail_szse()  # type: ignore[reportCallIssue]

            margin_data: Dict[str, Any] = {
                "stock_code": stock_code,
                "margin_balance": 0,
                "short_balance": 0,
                "margin_balance_change": 0,
                "short_balance_change": 0,
            }

            if not df.empty:
                latest = cast(Dict[str, Any], df.iloc[0].to_dict())
                prev = (
                    cast(Dict[str, Any], df.iloc[1].to_dict())
                    if len(df) > 1
                    else latest
                )

                margin_data["margin_balance"] = float(latest.get("融资余额", 0) or 0)
                margin_data["short_balance"] = float(latest.get("融券余额", 0) or 0)
                margin_data["margin_balance_change"] = margin_data[
                    "margin_balance"
                ] - float(prev.get("融资余额", 0) or 0)
                margin_data["short_balance_change"] = margin_data[
                    "short_balance"
                ] - float(prev.get("融券余额", 0) or 0)

            return margin_data

        except Exception as e:
            logger.error(
                f"[NorthboundFlowProvider] Failed to fetch margin balance: {e}"
            )
            return {}

    def calculate_flow_score(
        self,
        stock_code: str,
        days: int = 20,
    ) -> Dict[str, Any]:
        """
        Calculate flow score for capital positioning.

        Args:
            stock_code: Stock code
            days: Lookback period

        Returns:
            Score dict with components
        """
        flow_data = self.get_northbound_flow(
            symbol="北上", end_date=datetime.now().strftime("%Y%m%d")
        )

        if not flow_data:
            return {
                "stock_code": stock_code,
                "score": 50.0,
                "components": [],
                "northbound_flow_20d": 0,
                "updated_at": datetime.now().isoformat(),
            }

        flow_data = flow_data[:days] if len(flow_data) > days else flow_data

        total_net_inflow = sum(f.get("net_inflow", 0) for f in flow_data)
        avg_net_inflow = total_net_inflow / len(flow_data) if flow_data else 0

        positive_days = sum(1 for f in flow_data if f.get("net_inflow", 0) > 0)
        flow_ratio = positive_days / len(flow_data) if flow_data else 0.5

        score = 50.0
        components = []

        if avg_net_inflow > 1_000_000_000:
            score += 20
            components.append(("strong_inflow", 20))
        elif avg_net_inflow > 500_000_000:
            score += 10
            components.append(("moderate_inflow", 10))
        elif avg_net_inflow < -500_000_000:
            score -= 15
            components.append(("net_outflow", -15))

        if flow_ratio > 0.6:
            score += 10
            components.append(("consistent_buying", 10))
        elif flow_ratio < 0.4:
            score -= 10
            components.append(("consistent_selling", -10))

        score = max(0.0, min(100.0, score))

        return {
            "stock_code": stock_code,
            "score": score,
            "components": components,
            "northbound_flow_20d": avg_net_inflow / 1_000_000_000,
            "positive_days_ratio": flow_ratio,
            "total_net_inflow": total_net_inflow,
            "updated_at": datetime.now().isoformat(),
        }

    def _get_mock_northbound_flow(self) -> List[Dict[str, Any]]:
        """Return mock data when akshare is unavailable"""
        flows = []
        base_date = datetime.now()

        for i in range(20):
            date = base_date - timedelta(days=i)
            net_inflow = (i % 5 - 2) * 500_000_000

            flows.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "close": 100 + i * 0.5,
                    "change_pct": (i % 3 - 1) * 0.5,
                    "volume": 100_000_000 + i * 5_000_000,
                    "amount": 10_000_000_000 + i * 500_000_000,
                    "net_inflow": net_inflow,
                }
            )

        return flows


_northbound_flow_provider: Optional[NorthboundFlowProvider] = None


def get_northbound_flow_provider() -> NorthboundFlowProvider:
    """Get singleton provider"""
    global _northbound_flow_provider
    if _northbound_flow_provider is None:
        _northbound_flow_provider = NorthboundFlowProvider()
    return _northbound_flow_provider
