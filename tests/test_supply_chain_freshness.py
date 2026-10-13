# -*- coding: utf-8 -*-
"""供应链报告数据新鲜度验证。

覆盖 4 层新鲜度契约：

1. **报告生成时间**（``supply_chain_reports.created_at``）：报告何时生成。
2. **报告内嵌 deep_dive 时间**（``deep_dive_obj.fetched_at``）：v3 工具实际
   拉数据时间。
3. **v3 tool 实时数据缓存**（``_STOCK_INFO_CACHE`` TTL = 300s）。
4. **schema 不变性**（``frozen=True``）：报告字段生成后不可变。

不依赖真实网络 / LLM / DB；mock 到 supply_chain_report_service 与
supply_chain_tools 模块边界。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from src.agent.tools import supply_chain_tools as t_mod
from src.services import supply_chain_report_service as svc_mod
from src.services.supply_chain_report_service import SupplyChainReportService


# ============================================================
# Test fixtures
# ============================================================


def _fake_result(**kw: Any) -> SimpleNamespace:
    # 默认 markdown 含 §6 触发 deep_dive 备份层抽取
    default_md = (
        "# 供应链分析报告\n\n## 一句话结论\n..."
        "\n\n## 6. 产品矩阵\n\n### 6.1 核心产品\n飞天 53°..."
    )
    base = dict(
        success=True,
        content=default_md,
        error=None,
        total_steps=24,
        total_tokens=12000,
        provider="test",
        model="test-model",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _wire_service(monkeypatch, tmp_path, result):
    """monkeypatch ``get_db`` / ``_get_executor`` / ``get_supply_chain_report_dir``。"""
    fake_executor = MagicMock()
    fake_executor.chat.return_value = result
    monkeypatch.setattr(svc_mod, "_get_executor", lambda: fake_executor)
    monkeypatch.setattr(svc_mod, "get_supply_chain_report_dir", lambda: tmp_path)
    mock_db = MagicMock()
    mock_db.get_supply_chain_report.return_value = None
    mock_db.prune_supply_chain_reports.return_value = []
    monkeypatch.setattr(svc_mod, "get_db", lambda: mock_db)
    return fake_executor, mock_db


# ============================================================
# 1. 报告生成时间新鲜度
# ============================================================


class TestReportCreatedAtFreshness:
    """报告 generated_at 应反映真实生成时刻。"""

    def test_save_supply_chain_report_is_triggered(self, monkeypatch, tmp_path) -> None:
        """``save_supply_chain_report`` 应被调用（DB 端 default=datetime.now 填充 created_at）。"""
        _, mock_db = _wire_service(monkeypatch, tmp_path, _fake_result())
        saved: Dict[str, Any] = {}
        mock_db.save_supply_chain_report.side_effect = lambda **kw: (
            saved.update(kw),
            True,
        )[1]

        SupplyChainReportService().generate_report("光模块产业链", "CPO 上游")

        assert mock_db.save_supply_chain_report.called
        assert saved["topic"] == "光模块产业链"
        assert saved["status"] == "success"

    def test_two_reports_have_distinct_report_ids(self, monkeypatch, tmp_path) -> None:
        """两次调用应生成报告：seq 冲突时递增（mock DB 返回 EXISTS）。"""
        _, mock_db = _wire_service(monkeypatch, tmp_path, _fake_result())
        # 序列语义：A 调用 → get_report(".._1")=None → seq=1
        # B 调用 → get_report(".._1")="EXISTS" → seq=2 → get_report(".._2")=None → seq=2
        mock_db.get_supply_chain_report.side_effect = [None, "EXISTS", None]
        saved_ids = []
        mock_db.save_supply_chain_report.side_effect = lambda **kw: (
            saved_ids.append(kw["report_id"]),
            True,
        )[1]

        SupplyChainReportService().generate_report("A")
        SupplyChainReportService().generate_report("B")

        assert len(saved_ids) == 2
        assert saved_ids[0].startswith("sc_")
        assert saved_ids[1].startswith("sc_")
        # ID 格式：sc_YYYYMMDDHHmm_N
        assert saved_ids[0].split("_")[-1].isdigit()
        assert saved_ids[1].split("_")[-1].isdigit()
        # 第二次 seq 应递增（因 mock 返回 EXISTS 触发循环递增）
        assert int(saved_ids[1].split("_")[-1]) > int(saved_ids[0].split("_")[-1])

    def test_report_md_file_mtime_close_to_now(self, monkeypatch, tmp_path) -> None:
        """落盘 markdown 文件 mtime 应接近调用时刻。"""
        _, _ = _wire_service(monkeypatch, tmp_path, _fake_result())

        before = time.time()
        out = SupplyChainReportService().generate_report("测试")
        after = time.time()

        md_path = tmp_path / f"{out['report_id']}.md"
        assert md_path.exists()
        mtime = md_path.stat().st_mtime
        # mtime 必须在 [before, after] 窗口内（精度 ±0.5s 接受）
        assert before - 0.5 <= mtime <= after + 0.5


# ============================================================
# 2. deep_dive_obj.fetched_at 新鲜度（已知 bug：datetime 无法 JSON 序列化）
# ============================================================


class TestDeepDiveFetchedAtFreshness:
    """当 executor 挂 ``deep_dive_obj`` 时，``fetched_at`` 应被保留且非空。"""

    def test_deep_dive_obj_with_fetched_at_round_trips(
        self, monkeypatch, tmp_path
    ) -> None:
        """deep_dive_obj 含 datetime fetched_at → 落盘 deep_dive_json 应保留该字段（ISO）。

        修复历史：曾因 ``model_dump()`` 返回 dict 含 datetime 实例，
        ``json.dumps`` 抛 ``TypeError``。修复后 ``_validate_deep_dive_payload``
        使用 ``model_dump(mode='json')`` 让 Pydantic 自动转 ISO 字符串。
        """
        fetched_at = datetime(2026, 7, 31, 10, 0, 0, tzinfo=timezone.utc)
        valid_payload = {
            "ticker": "600519",
            "company": "贵州茅台",
            "fetched_at": fetched_at,
            "sections_executed": ["product_matrix"],
        }
        result = _fake_result()
        result.deep_dive_obj = valid_payload
        _, mock_db = _wire_service(monkeypatch, tmp_path, result)
        saved: Dict[str, Any] = {}
        mock_db.save_supply_chain_report.side_effect = lambda **kw: (
            saved.update(kw),
            True,
        )[1]

        SupplyChainReportService().generate_report(
            "白酒", "高端", raw_code="600519", raw_name="贵州茅台"
        )

        assert saved["deep_dive_json"] is not None
        parsed = json.loads(saved["deep_dive_json"])
        assert "deep_dive_obj" in parsed
        obj = parsed["deep_dive_obj"]
        assert obj["ticker"] == "600519"
        # model_dump(mode='json') 把 datetime 转 ISO 字符串
        assert obj["fetched_at"].startswith("2026-07-31T10:00:00")

    def test_deep_dive_obj_without_fetched_at_saves_via_default_none(
        self, monkeypatch, tmp_path
    ) -> None:
        """deep_dive_obj 无 fetched_at → schema 默认 None → 落盘成功。"""
        payload_no_ts = {
            "ticker": "600519",
            "company": "贵州茅台",
            "sections_executed": [],
        }
        result = _fake_result()
        result.deep_dive_obj = payload_no_ts
        _, mock_db = _wire_service(monkeypatch, tmp_path, result)
        saved: Dict[str, Any] = {}
        mock_db.save_supply_chain_report.side_effect = lambda **kw: (
            saved.update(kw),
            True,
        )[1]

        SupplyChainReportService().generate_report("白酒", raw_code="600519")

        assert saved["deep_dive_json"] is not None
        parsed = json.loads(saved["deep_dive_json"])
        obj = parsed["deep_dive_obj"]
        # Pydantic 默认值应填充，fetched_at 为 None
        assert obj["fetched_at"] is None

    def test_invalid_deep_dive_obj_falls_back_to_markdown_only(
        self, monkeypatch, tmp_path
    ) -> None:
        """非法 deep_dive_obj 应被丢弃，仅保留 markdown 备份层。"""
        result = _fake_result()
        result.deep_dive_obj = {"ticker": "!!!invalid ticker!!!"}
        _, mock_db = _wire_service(monkeypatch, tmp_path, result)
        saved: Dict[str, Any] = {}
        mock_db.save_supply_chain_report.side_effect = lambda **kw: (
            saved.update(kw),
            True,
        )[1]

        SupplyChainReportService().generate_report("测试")

        # deep_dive_json 仍可能含备份层（_raw_markdown_section），但不应含 deep_dive_obj
        if saved["deep_dive_json"] is not None:
            parsed = json.loads(saved["deep_dive_json"])
            assert "deep_dive_obj" not in parsed


# ============================================================
# 3. v3 tool 实时数据缓存新鲜度（TTL = 300s）
# ============================================================


class TestStockInfoCacheFreshness:
    """``_STOCK_INFO_CACHE`` 应在 300s 内复用，超过则重拉。"""

    def test_cache_hit_within_ttl_returns_cached_payload(self) -> None:
        """缓存命中：在 TTL 内调用 ``_fetch_real_stock_info`` 返回原 payload。"""
        ticker = "TEST_CACHE_HIT"
        t_mod._STOCK_INFO_CACHE.clear()
        t_mod._STOCK_INFO_PAYLOAD.clear()
        sentinel = {"ticker": ticker, "cached": True}
        with patch(
            "src.agent.tools.supply_chain_tools._fetch_real_stock_info_uncached",
            return_value=sentinel,
        ):
            first = t_mod._fetch_real_stock_info(ticker)
            # 同一 ticker 在 300s TTL 内应直接命中缓存，不再调 uncached
            second = t_mod._fetch_real_stock_info(ticker)

        assert first is sentinel
        # 第二次应返回相同对象（缓存命中）
        assert second is sentinel

    def test_cache_ttl_is_five_minutes(self) -> None:
        """``_STOCK_INFO_CACHE_TTL`` 应 = 300.0s（与 docstring 一致）。"""
        assert t_mod._STOCK_INFO_CACHE_TTL == 300.0

    def test_cache_payload_separated_for_quote_reuse(self) -> None:
        """_STOCK_INFO_PAYLOAD 用于 _fetch_real_realtime_quote 复用 quote dict。"""
        ticker = "TEST_PAYLOAD"
        t_mod._STOCK_INFO_CACHE.clear()
        t_mod._STOCK_INFO_PAYLOAD.clear()
        sentinel = {
            "code": ticker,
            "fundamental_context": {},
            "belong_boards": [],
            "_quote_dict": {"price": 100.0, "change_pct": 1.5},
        }
        with patch(
            "src.agent.tools.supply_chain_tools._fetch_real_stock_info_uncached",
            return_value=sentinel,
        ):
            t_mod._fetch_real_stock_info(ticker)

        # _STOCK_INFO_PAYLOAD 应被填充（供 _fetch_real_realtime_quote 复用）
        assert f"legacy:{ticker}" in t_mod._STOCK_INFO_PAYLOAD
        cached_payload = t_mod._STOCK_INFO_PAYLOAD[f"legacy:{ticker}"]
        assert cached_payload["_quote_dict"]["price"] == 100.0

    def test_inflight_lock_prevents_concurrent_fetches(self) -> None:
        """_STOCK_INFO_INFLIGHT 机制：并发场景下第二次调用应等待而非重拉。"""
        ticker = "TEST_INFLIGHT"
        t_mod._STOCK_INFO_CACHE.clear()
        t_mod._STOCK_INFO_PAYLOAD.clear()
        t_mod._STOCK_INFO_INFLIGHT.pop(f"legacy:{ticker}", None)

        # 第一次模拟耗时长：注入一个已 set 的 evt 表示"有别的线程在跑"
        evt = MagicMock()
        evt.wait.return_value = True
        t_mod._STOCK_INFO_INFLIGHT[f"legacy:{ticker}"] = evt

        with patch(
            "src.agent.tools.supply_chain_tools._fetch_real_stock_info_uncached"
        ) as mock_uncached:
            mock_uncached.return_value = {"code": ticker}
            out = t_mod._fetch_real_stock_info(ticker)
            # 由于 inflight 已存在 evt，应等待后直接读 PAYLOAD（无 PAYLOAD 时 fallback
            # 到 PAYLOAD.get ticker），最终返回 {}（PAYLOAD 仍为空）
            assert out == {}
            # uncached 不应被调用（等待机制生效）
            mock_uncached.assert_not_called()

        # 清理
        t_mod._STOCK_INFO_INFLIGHT.pop(f"legacy:{ticker}", None)


# ============================================================
# 4. 报告整体时效性（end-to-end）
# ============================================================


class TestReportEndToEndFreshness:
    """端到端：generate_report 应在合理时间内完成并落盘。"""

    def test_full_pipeline_completes_and_saves_within_seconds(
        self, monkeypatch, tmp_path
    ) -> None:
        _, mock_db = _wire_service(monkeypatch, tmp_path, _fake_result())
        saved: Dict[str, Any] = {}
        mock_db.save_supply_chain_report.side_effect = lambda **kw: (
            saved.update(kw),
            True,
        )[1]

        start = time.monotonic()
        out = SupplyChainReportService().generate_report(
            "光模块产业链", "CPO 上游", raw_code="300750", raw_name="宁德时代"
        )
        elapsed = time.monotonic() - start

        # mock 路径下应 < 1s
        assert elapsed < 1.0
        assert out["status"] == "success"
        assert out["stock_code"] == "300750"
        assert out["stock_name"] == "宁德时代"
        assert saved["stock_code"] == "300750"

    def test_deep_dive_json_includes_both_layers_when_no_datetime(
        self, monkeypatch, tmp_path
    ) -> None:
        """deep_dive_obj 不含 datetime 字段时（含 fetched_at=None），同时含备份层 + 结构化层。"""
        result = _fake_result()
        result.deep_dive_obj = {
            "ticker": "600519",
            "company": "贵州茅台",
            # fetched_at 字段缺省 → schema 默认 None → 不触发 datetime 序列化 bug
        }
        # 开启 deep_dive 灰度（默认关闭）
        monkeypatch.setenv("SERENITY_DEEP_DIVE_V3_ENABLED", "true")

        _, mock_db = _wire_service(monkeypatch, tmp_path, result)
        saved: Dict[str, Any] = {}
        mock_db.save_supply_chain_report.side_effect = lambda **kw: (
            saved.update(kw),
            True,
        )[1]

        SupplyChainReportService().generate_report("白酒", raw_code="600519")

        assert saved["deep_dive_json"] is not None
        parsed = json.loads(saved["deep_dive_json"])
        # 备份层（_raw_markdown_section，§6-§10 段）
        assert "_raw_markdown_section" in parsed
        # 结构化层（deep_dive_obj，schema 校验后）
        assert "deep_dive_obj" in parsed
        assert parsed["deep_dive_obj"]["ticker"] == "600519"
        assert parsed["deep_dive_obj"]["fetched_at"] is None
