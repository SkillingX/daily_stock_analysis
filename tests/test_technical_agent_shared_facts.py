# -*- coding: utf-8 -*-
"""技术研究共享事实契约测试（issue #42）。

共享快照已有日线时缠论引擎复用之，不再重复取数；
任务文本注入共享行情事实，禁止研究员声称这些数据缺失。
"""

from __future__ import annotations

from unittest.mock import patch

from src.agent.deep_research import explore_agents
from src.deep_research_dims.context import SharedContext


def _history(days: int = 100, price: float = 10.0) -> list[dict]:
    return [
        {"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": price, "high": price + 1,
         "low": price - 1, "close": price, "volume": 1000}
        for i in range(days)
    ]


class TestChanlunEngineFactsReuse:
    def test_shared_history_skips_loader(self):
        """共享快照 ≥60 根时不再调用 load_history_df。"""
        with patch("src.services.history_loader.load_history_df") as mock_load:
            facts = explore_agents._chanlun_engine_facts(
                "600519", history=_history(100)
            )
        mock_load.assert_not_called()
        # 引擎结果或空串都可接受，关键是取数路径未触发
        assert isinstance(facts, str)

    def test_no_shared_history_falls_back_to_loader(self):
        with patch(
            "src.services.history_loader.load_history_df",
            return_value=(None, "none"),
        ):
            facts = explore_agents._chanlun_engine_facts("600519", history=None)
        assert facts == ""

    def test_short_shared_history_falls_back_to_loader(self):
        with patch(
            "src.services.history_loader.load_history_df",
            return_value=(None, "none"),
        ) as mock_load:
            explore_agents._chanlun_engine_facts("600519", history=_history(30))
        mock_load.assert_called_once()


class TestSharedMarketFacts:
    def _ctx(self) -> SharedContext:
        ctx = SharedContext("600519", "贵州茅台", "2026-10-23T15:00:00")
        ctx.quote = {"price": 162.16}
        ctx.history = _history(260)
        ctx.history_source = "db_cache"
        return ctx

    def test_facts_contain_price_and_history(self):
        facts = explore_agents._shared_market_facts(self._ctx())
        assert "162.16" in facts
        assert "260" in facts
        assert "2026-" in facts  # 最新收盘的真实数据日期（非装配时间）

    def test_facts_include_computed_indicators_and_real_source(self):
        """审查 P1-2：MA20、支撑阻力与实际来源必须传入，不得概括为装配时点。"""
        facts = explore_agents._shared_market_facts(self._ctx())
        assert "MA20" in facts
        assert "支撑" in facts and "阻力" in facts
        assert "db_cache" in facts
        assert "15:00:00" not in facts  # 装配时间不再冒充数据日期

    def test_empty_context_gives_nothing(self):
        assert explore_agents._shared_market_facts(None) == ""
        empty = SharedContext("600519", "x", "2026-10-23")
        facts = explore_agents._shared_market_facts(empty)
        assert "MA20" not in facts

    def test_task_forbids_claiming_shared_data_missing(self):
        ctx = self._ctx()
        market = explore_agents._shared_market_facts(ctx)
        task = explore_agents._technical_task("600519", "贵州茅台", market, "引擎结构fixture")
        assert "禁止声称这些行情缺失" in task
        assert "禁止把局部缺项说成整套行情缺失" in task
        assert "162.16" in task
        assert "MA20" in task
        assert "支撑" in task
