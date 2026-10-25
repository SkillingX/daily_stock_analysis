# -*- coding: utf-8 -*-
"""Tests for P2 PR-B4: compass rewriter wiring in the pipeline guardrail chain."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.analyzer import AnalysisResult
from src.core.pipeline import StockAnalysisPipeline
from src.services.compass.engine import EngineOutput


def _result(**overrides: object) -> AnalysisResult:
    base: dict[str, object] = {
        "code": "600519",
        "name": "贵州茅台",
        "sentiment_score": 62,
        "trend_prediction": "看多",
        "operation_advice": "买入",
        "decision_type": "buy",
        "action": "buy",
    }
    base.update(overrides)
    return AnalysisResult(**base)  # type: ignore[arg-type]


def _engine_output(**overrides: object) -> EngineOutput:
    base: dict[str, object] = {
        "l0": "weekly_bull",
        "l1": "annual_bull",
        "l2": "alive",
        "l3": "healthy",
        "phase": "trend_expanding",
        "observe_horizon": "1w",
        "position_filter": "full",
        "weekly_ema50": 100.0,
        "weekly_ema200": 90.0,
        "weekly_sample_size": 120,
        "daily_sample_size": 300,
        "ema20": 101.0,
        "ema50": 100.0,
        "ema100": 95.0,
        "ema200": 90.0,
        "rsi14": 55.0,
        "slope_ema20_10d": 0.5,
        "slope_ema50_20d": 0.3,
        "slope_ema200_40d": 0.1,
        "last_close": 102.0,
        "cross_ema20_ema50": "above",
    }
    base.update(overrides)
    return EngineOutput(**base)  # type: ignore[arg-type]


def _daily_closes(last_bar: date) -> pd.Series:
    dates = pd.date_range(end=pd.Timestamp(last_bar), periods=60, freq="B")
    return pd.Series(range(60), index=dates, dtype=float, name="close")


def _make_pipeline(*, compass_enabled: bool, snapshot_write: bool = True) -> StockAnalysisPipeline:
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(
        compass_enabled=compass_enabled,
        report_language="zh",
    )
    pipeline.fetcher_manager = MagicMock()
    pipeline.compass_snapshot_write = snapshot_write
    pipeline.analysis_skills = None
    return pipeline


class TestCompassDisabled:
    def test_noop_when_disabled(self) -> None:
        pipeline = _make_pipeline(compass_enabled=False)
        result = _result()
        with patch("src.services.compass.fetcher.fetch_for_compass") as fetch_mock:
            pipeline._apply_midterm_compass(
                result, "600519", prior_softened=False, prior_suppressed=False
            )
        fetch_mock.assert_not_called()
        assert result.midtrend_compass is None
        assert result.action == "buy"


class TestExcludedCodes:
    def test_market_code_skipped(self) -> None:
        pipeline = _make_pipeline(compass_enabled=True)
        result = _result(code="MARKET")
        with patch("src.services.compass.fetcher.fetch_for_compass") as fetch_mock:
            pipeline._apply_midterm_compass(
                result, "MARKET", prior_softened=False, prior_suppressed=False
            )
        fetch_mock.assert_not_called()
        assert result.midtrend_compass is None


class TestRewriterRunsInChain:
    def _run_helper(
        self,
        engine: EngineOutput,
        *,
        result: AnalysisResult,
        last_bar: date,
        code: str = "600519",
        prior_softened: bool = False,
    ) -> AnalysisResult:
        pipeline = _make_pipeline(compass_enabled=True)
        daily = _daily_closes(last_bar)
        weekly = pd.Series([1.0, 2.0], index=pd.date_range("2024-01-05", periods=2, freq="W-FRI"))
        # infer_bar_status 依赖当前时刻；测试中固定为 closed（15:30 后真实行为），
        # 盘中场景由 test_intraday_bar_blocks_buy 单独打桩。
        with patch(
            "src.services.compass.fetcher.fetch_for_compass",
            return_value=(daily, weekly, "mock"),
        ), patch(
            "src.services.compass.engine.compute", return_value=engine
        ), patch(
            "src.services.compass.rewriter.infer_bar_status", return_value="closed"
        ):
            pipeline._apply_midterm_compass(
                result, code, prior_softened=prior_softened, prior_suppressed=False
            )
        return result

    def test_weekly_bear_rewrites_buy_to_watch(self) -> None:
        result = self._run_helper(
            _engine_output(l0="weekly_bear", phase="trend_holding"),
            result=_result(),
            last_bar=date.today() - timedelta(days=1),
        )
        assert result.action == "watch"
        assert result.decision_type == "hold"
        assert result.operation_advice == "观望"
        assert result.midtrend_compass is not None
        assert result.midtrend_compass["final_action"] == "watch"
        assert "weekly_bear_buy_blocked" in result.midtrend_compass["action_reason_codes"]
        assert result.midtrend_compass["bar_status"] == "closed"

    def test_intraday_bar_blocks_buy(self) -> None:
        pipeline = _make_pipeline(compass_enabled=True)
        daily = _daily_closes(date.today())
        weekly = pd.Series([1.0, 2.0], index=pd.date_range("2024-01-05", periods=2, freq="W-FRI"))
        result = _result()
        with patch(
            "src.services.compass.fetcher.fetch_for_compass",
            return_value=(daily, weekly, "mock"),
        ), patch(
            "src.services.compass.engine.compute", return_value=_engine_output()
        ), patch(
            "src.services.compass.rewriter.infer_bar_status",
            return_value="intraday_unconfirmed",
        ):
            pipeline._apply_midterm_compass(
                result, "600519", prior_softened=False, prior_suppressed=False
            )
        assert result.action == "watch"
        assert "intraday_unconfirmed_buy_blocked" in (
            result.midtrend_compass["action_reason_codes"]
        )

    def test_no_rewrite_leaves_result_fields_untouched(self) -> None:
        # Clean bull state + buy draft: no hard rule fires, so the original
        # eight-state fields must NOT be clobbered by the 3-state mapping.
        result = self._run_helper(
            _engine_output(),
            result=_result(),
            last_bar=date.today() - timedelta(days=1),
        )
        assert result.action == "buy"
        assert result.decision_type == "buy"
        assert result.operation_advice == "买入"
        # Snapshot still records the evaluation for cron audit.
        assert result.midtrend_compass is not None
        assert result.midtrend_compass["final_action"] == "buy"

    def test_sell_side_rewrite_writes_investment_conclusion(self) -> None:
        result = _result(
            action="watch", operation_advice="观望", decision_type="hold",
            investment_conclusion={"action": "观察"},
        )
        self._run_helper(
            _engine_output(l1="annual_bear", l3="cooling", phase="trend_holding"),
            result=result,
            last_bar=date.today() - timedelta(days=1),
        )
        assert result.action == "sell"
        assert result.decision_type == "sell"
        assert result.investment_conclusion["action"] == "止损"
        assert "l1_l2_bear_sell_default" in result.midtrend_compass["action_reason_codes"]

    def test_passthrough_reason_recorded(self) -> None:
        result = self._run_helper(
            _engine_output(),
            result=_result(action="watch", operation_advice="观望", decision_type="hold"),
            last_bar=date.today() - timedelta(days=1),
            prior_softened=True,
        )
        # Draft watch + clean state + prior market guardrail softening.
        assert result.midtrend_compass["action_reason_codes"] == [
            "market_guardrail_softened"
        ]

    def test_fetch_failure_passes_through(self) -> None:
        pipeline = _make_pipeline(compass_enabled=True)
        result = _result()
        with patch(
            "src.services.compass.fetcher.fetch_for_compass",
            side_effect=RuntimeError("network down"),
        ):
            pipeline._apply_midterm_compass(
                result, "600519", prior_softened=False, prior_suppressed=False
            )
        assert result.action == "buy"
        assert result.midtrend_compass is None

    def test_insufficient_bars_skips_quietly(self) -> None:
        pipeline = _make_pipeline(compass_enabled=True)
        result = _result()
        daily = _daily_closes(date.today() - timedelta(days=1)).iloc[:10]
        weekly = pd.Series(dtype=float)
        with patch(
            "src.services.compass.fetcher.fetch_for_compass",
            return_value=(daily, weekly, "mock"),
        ):
            pipeline._apply_midterm_compass(
                result, "600519", prior_softened=False, prior_suppressed=False
            )
        assert result.midtrend_compass is None
        assert result.action == "buy"


class TestHistorySnapshotPersistsMidtrendCompass:
    def _snapshot(
        self, *, snapshot_write: bool, compass: dict | None
    ) -> dict:
        pipeline = _make_pipeline(compass_enabled=True, snapshot_write=snapshot_write)
        return pipeline._build_context_snapshot(
            enhanced_context={},
            news_content=None,
            realtime_quote=None,
            midtrend_compass=compass,
        )

    def test_cron_path_persists(self) -> None:
        compass = {"code": "600519", "final_action": "watch"}
        snapshot = self._snapshot(snapshot_write=True, compass=compass)
        assert snapshot["midtrend_compass"] == compass

    def test_web_manual_path_does_not_persist(self) -> None:
        compass = {"code": "600519", "final_action": "watch"}
        snapshot = self._snapshot(snapshot_write=False, compass=compass)
        assert "midtrend_compass" not in snapshot

    def test_cron_without_compass_result_leaves_key_absent(self) -> None:
        snapshot = self._snapshot(snapshot_write=True, compass=None)
        assert "midtrend_compass" not in snapshot
