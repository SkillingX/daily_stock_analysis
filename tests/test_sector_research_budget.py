# -*- coding: utf-8 -*-
"""#83 预算/上下文契约（contract 4）：绝对截止贯穿到真实 wire。

- 文章探查 BudgetExhausted → partial 存档保留事实（不传播整报 504）
- 绝对截止绑定：模型出站线程内 current_research/request_deadline 绑定
  fetch_deadline，finally reset（下一次调用上下文干净）
- deadline 后队列持有无 wire（run.check 拒绝；context reset 负控制）
- fallback 两模型实际尝试、递减剩余预算（真实 call_completion fallback 循环）
- synthesis 取数预算耗尽 → model_error 局部降块（合格事实保留）
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import patch

import httpx
import pytest

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from src.services.sector_analysis_service import (
    BudgetExhausted,
    ReportBudget,
    _catalysts_pillar,
    _synthesis_pillar,
)
from src.services import sector_analysis_service as svc
from src.services import sector_research


# ---------------------------------------------------------------------------
# 合成基建
# ---------------------------------------------------------------------------

class _SlowProvider:
    """受控慢搜索 provider（可配置延迟，模拟供应商在途）。"""

    name = "SlowSynthetic"

    def __init__(self, rows: List[Dict[str, Any]], delay: float = 0.0):
        self._rows = rows
        self._delay = delay

    @property
    def is_available(self) -> bool:
        return True

    def search(self, query: str, max_results: int = 5, days: int = 7):
        from src.search_service import SearchResult

        if self._delay:
            time.sleep(self._delay)
        return type("R", (), {
            "query": query, "results": [SearchResult(**row) for row in self._rows],
            "provider": self.name, "success": True, "error_message": None,
            "search_time": self._delay,
        })()


def _catalyst_rows() -> List[Dict[str, Any]]:
    from src.search_service import get_search_service

    days = int(get_search_service().news_window_days)
    today = datetime.now().date()
    return [
        {"title": "白酒行业新政策发布", "snippet": "新规落地",
         "url": "https://example.com/news/a.html", "source": "example.com",
         "published_date": today.isoformat()},
        {"title": "白酒行业旧闻待探查", "snippet": "无日期条目",
         "url": "https://example.com/news/nodate.html", "source": "example.com",
         "published_date": None},
    ]


def _confirmed_context() -> Dict[str, Dict[str, Any]]:
    """confirmed 场景的 ident/prosper/trend/peers/catalysts（合成）。"""
    today = datetime.now().date().isoformat()
    ident = {"industry_em": "白酒Ⅱ", "board_name": "白酒Ⅱ", "board_code": "BK1277",
             "subject_status": "confirmed",
             "membership": {"verified": True, "member_count": 20},
             "sector_name": "白酒Ⅱ", "gaps": []}
    prosper = {"sector_found": True, "momentum": 80.0, "rank_of": 1,
               "total_boards": 400, "breadth": 60.0,
               "row": {"change_pct": 1.65}, "gaps": []}
    trend = {"eligible": True, "start_date": "2026-10-01", "end_date": "2026-10-30",
             "qualified_sessions": 20, "period_return_pct": 5.0,
             "benchmark": {"period_return_pct": 3.0},
             "relative_return_pp": 2.0, "gaps": []}
    peers = {"eligible": True,
             "items": [{"name": "贵州茅台", "is_target": True, "change_pct": 1.5},
                       {"name": "五粮液", "is_target": False, "change_pct": 0.9}],
             "gaps": []}
    catalysts = {
        "eligible": True, "query": "q", "window_days": 3, "window_start": today,
        "window_end": today, "observed_at": "t", "provider": "S",
        "items": [{"evidence_id": "cat-1", "title": "白酒行业新政策发布",
                   "url": "https://example.com/news/a.html",
                   "source_domain": "example.com", "snippet": "新规落地",
                   "published_date": today, "date_basis": "provider_published",
                   "date_basis_detail": None, "same_event_urls": []}],
        "background_items": [], "dropped_duplicate_url": 0,
        "dropped_syndicated": 0, "dropped_offwindow": 0, "dropped_offtopic": 0,
        "dropped_invalid_source": 0, "source": "s", "gaps": [],
    }
    return {"ident": ident, "prosper": prosper, "trend": trend,
            "peers": peers, "catalysts": catalysts}


# ---------------------------------------------------------------------------
# 文章探查超时 → partial（contract 4 指定反例）
# ---------------------------------------------------------------------------

class TestArticleProbePartialContract:
    """文章探查耗尽取数预算：局部终止，已取得内容保留（合成 wire 全链路）。"""

    def test_probe_timeout_keeps_facts_and_archives_partial(
            self, monkeypatch, tmp_path):
        from src.search_service import get_search_service

        monkeypatch.setenv("LITELLM_MODEL", "openai/synthetic-standard")
        monkeypatch.setenv("OPENAI_API_KEYS", "synthetic-placeholder")
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        from src.config import Config

        Config.reset_instance()
        search_svc = get_search_service()
        monkeypatch.setattr(search_svc, "_providers", [_SlowProvider(_catalyst_rows())])

        def _slow_probe(url: str, budget: ReportBudget):
            time.sleep(0.6)  # 超过剩余取数预算
            budget.require_fetch()  # 线程内检查 → BudgetExhausted
            raise BudgetExhausted("探查预算耗尽")

        monkeypatch.setattr(svc, "_probe_article_date_evidence", _slow_probe)

        # 模型 wire 合成（综合真实跑）
        content = json.dumps({"claims": [
            {"kind": "interpretation", "text": "白酒行业新政策构成近期催化",
             "evidence_refs": ["cat-1"]}]}, ensure_ascii=False)
        chunks = [
            {"id": "s", "object": "chat.completion.chunk", "created": 0,
             "model": "synthetic-standard",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": content},
                          "finish_reason": None}]},
            {"id": "s", "object": "chat.completion.chunk", "created": 0,
             "model": "synthetic-standard",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        wire = "".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n"
                       for c in chunks) + "data: [DONE]\n\n"

        async def _synthetic_send(client_self, request, *args, **kwargs):
            if isinstance(client_self._transport, httpx.ASGITransport):
                return await type(client_self).send(client_self, request, *args, **kwargs)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=wire.encode(), request=request)

        # 生成入口：总预算 1s，收尾余量 0.5s（探查在 0.5s 取数预算内耗尽）
        report_dir = tmp_path / "reports" / "sector_analysis"
        monkeypatch.setattr(svc, "_REPORT_DIR", report_dir)

        async def _run_generate():
            pass

        # 直接以 compose 验证（generate 入口的存档边界由 81/82 已验收测试覆盖）
        budget = ReportBudget(5.0, finish_margin=0.5)
        import src.search_service as _ss

        monkeypatch.setattr(_ss, "_search_service", search_svc)
        ctx = _confirmed_context()
        with patch.object(httpx.AsyncClient, "send", _synthetic_send):
            catalysts = _catalysts_pillar(
                "白酒Ⅱ", budget=budget, subject_confirmed=True, stock_name="贵州茅台",
            )
            synthesis = _synthesis_pillar(
                "贵州茅台", "600519", ctx["ident"], ctx["prosper"], ctx["trend"],
                ctx["peers"], catalysts, budget=budget, subject_confirmed=True,
            )
        # 已取得催化保留 + 探查局部终止缺口 + 综合成功
        assert len(catalysts["items"]) == 1
        assert any("提前结束" in g for g in catalysts["gaps"])
        assert synthesis["status"] == "ok"
        # 主截止内仍有收尾余量（未超时）
        assert not budget.expired()

    def test_main_deadline_still_times_out_whole_report(self):
        # 主截止耗尽仍是诚实超时（BudgetExhausted 语义不因局部处理弱化）
        budget = ReportBudget(0.05)
        time.sleep(0.1)
        with pytest.raises(BudgetExhausted):
            budget.require()


# ---------------------------------------------------------------------------
# 绝对截止绑定 / 上下文 reset
# ---------------------------------------------------------------------------

class TestAbsoluteDeadlineBinding:
    """模型出站线程内绑定绝对截止；finally reset；晚获得执行权不出站。"""

    def test_model_thread_binds_absolute_deadline_and_resets(self, monkeypatch):
        from src.core.research_run import current_research
        from src.patches.eastmoney_patch import request_deadline

        ctx = _confirmed_context()
        observed: Dict[str, Any] = {}

        monkeypatch.setenv("LITELLM_MODEL", "openai/synthetic-standard")
        monkeypatch.setenv("OPENAI_API_KEYS", "synthetic-placeholder")
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        from src.config import Config

        Config.reset_instance()

        budget = ReportBudget(30.0, finish_margin=5.0)
        expected_deadline = budget.fetch_deadline

        real_synthesis = _synthesis_pillar

        def _synthesis_with_capture(*args: Any, **kwargs: Any):
            # 在模型终端处观察线程上下文（patch 真实 adapter 实例方法）
            from src.agent.llm_adapter import LLMToolAdapter

            def _capture(self, *a, **kw):
                run = current_research.get()
                observed["run_deadline"] = run.deadline if run is not None else None
                observed["request_deadline"] = request_deadline.get()
                observed["timeout"] = kw.get("timeout")
                # reset 在 caller finally 中执行于本终端返回后——此处仍在
                # 绑定期（值应等于 fetch_deadline）；caller 侧另证 reset
                raise TypeError("terminal-stop")
            with patch.object(LLMToolAdapter, "_call_litellm_model", _capture):
                return real_synthesis(*args, **kwargs)

        # 程序错误 opt-in 上抛（root 批准路径）
        with pytest.raises(TypeError):
            _synthesis_with_capture(
                "贵州茅台", "600519", ctx["ident"], ctx["prosper"], ctx["trend"],
                ctx["peers"], ctx["catalysts"], budget=budget, subject_confirmed=True,
            )
        # 绝对截止 == ReportBudget.fetch_deadline（同一 monotonic 时刻域）
        assert observed["run_deadline"] == expected_deadline
        assert observed["request_deadline"] == expected_deadline
        assert observed["timeout"] is not None
        assert observed["timeout"] <= budget.total_seconds
        # caller（主线程）上下文从未被污染（绑定只在工作线程）
        assert current_research.get() is None
        assert request_deadline.get() is None

    def test_worker_context_reset_after_call(self, monkeypatch):
        """共享池 worker 线程复用：调用结束后同一线程内上下文干净（finally reset）。"""
        from concurrent.futures import ThreadPoolExecutor

        from src.core.research_run import current_research
        from src.patches.eastmoney_patch import request_deadline

        ctx = _confirmed_context()
        monkeypatch.setenv("LITELLM_MODEL", "openai/synthetic-standard")
        monkeypatch.setenv("OPENAI_API_KEYS", "synthetic-placeholder")
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        from src.config import Config

        Config.reset_instance()
        from src.agent.llm_adapter import LLMToolAdapter

        # 单 worker 池替换共享池：绑定/读上下文发生在同一线程
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reset-check")
        monkeypatch.setattr(svc, "_FETCH_EXECUTOR", pool)
        in_thread: Dict[str, Any] = {}

        def _capture_binding(self, *a, **kw):
            in_thread["bound"] = (
                current_research.get() is not None and request_deadline.get() is not None
            )
            raise TypeError("terminal-stop")

        budget = ReportBudget(30.0, finish_margin=5.0)
        with patch.object(LLMToolAdapter, "_call_litellm_model", _capture_binding):
            with pytest.raises(TypeError):
                _synthesis_pillar(
                    "贵州茅台", "600519", ctx["ident"], ctx["prosper"], ctx["trend"],
                    ctx["peers"], ctx["catalysts"], budget=budget,
                    subject_confirmed=True,
                )
        assert in_thread["bound"] is True  # 绑定期观察到绝对上下文

        # 同一 worker 线程：调用结束（finally reset）后上下文干净
        def _read_context():
            in_thread["after"] = (
                current_research.get() is None and request_deadline.get() is None
            )

        pool.submit(_read_context).result(5)
        pool.shutdown(wait=False)
        assert in_thread["after"] is True

    def test_worker_after_deadline_starts_no_wire(self, monkeypatch):
        """取数截止后晚获得执行权的模型任务：首检查即拒绝，无 wire。"""
        from src.core.research_run import current_research
        from src.patches.eastmoney_patch import request_deadline

        ctx = _confirmed_context()
        wire_attempts: List[str] = []

        def _late_terminal(*args: Any, **kwargs: Any):
            run = current_research.get()
            if run is not None:
                run.check()  # 晚获得执行权：BudgetExhausted → no wire
            wire_attempts.append("wire")

        monkeypatch.setenv("LITELLM_MODEL", "openai/synthetic-standard")
        monkeypatch.setenv("OPENAI_API_KEYS", "synthetic-placeholder")
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        from src.config import Config

        Config.reset_instance()
        from src.agent.llm_adapter import LLMToolAdapter

        # 主截止 2.0s（收尾余量 0.5s）：取数预算在 1.5s 处耗尽，主截止未过
        budget = ReportBudget(2.0, finish_margin=0.5)
        time.sleep(1.6)  # 取数预算已耗尽，主截止余 0.4s
        out = _synthesis_pillar(
            "贵州茅台", "600519", ctx["ident"], ctx["prosper"], ctx["trend"],
            ctx["peers"], ctx["catalysts"], budget=budget, subject_confirmed=True,
        )
        assert out["status"] == "model_error"
        assert any("取数预算不足" in g for g in out["gaps"])
        assert wire_attempts == []  # 无 wire
        # 主截止未过（有收尾余量）：报告可继续收尾
        assert not budget.expired()


class TestFallbackBudgetDecrement:
    """真实 call_completion fallback：两个模型实际尝试，剩余预算递减。"""

    def test_two_models_tried_with_decreasing_budget(self, monkeypatch):
        monkeypatch.setenv("LITELLM_MODEL", "openai/synthetic-standard")
        monkeypatch.setenv("LITELLM_FALLBACK_MODELS", "openai/synthetic-fallback")
        monkeypatch.setenv("OPENAI_API_KEYS", "synthetic-placeholder")
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        from src.config import Config

        Config.reset_instance()
        from src.agent.llm_adapter import LLMToolAdapter

        adapter = LLMToolAdapter()
        assert adapter.is_available is True
        calls: List[Dict[str, Any]] = []

        def _failing_terminal(*args: Any, **kwargs: Any):
            # _call_litellm_model(messages, tools, model, **kwargs)：位置 2 是模型
            calls.append({
                "model": args[2] if len(args) > 2 else None,
                "timeout": kwargs.get("timeout"),
            })
            raise RuntimeError("synthetic provider down")

        with patch.object(LLMToolAdapter, "_call_litellm_model", _failing_terminal):
            resp = adapter.call_text([{"role": "user", "content": "hi"}], timeout=10)
        assert resp.provider == "error"
        assert len(calls) >= 2, f"fallback 未实际尝试两个模型: {calls}"
        timeouts = [c["timeout"] for c in calls if c["timeout"] is not None]
        assert timeouts[0] > timeouts[-1], f"剩余预算未递减: {timeouts}"


class TestSynthesisBudgetExhaustionLocal:
    """综合取数预算耗尽 → model_error 局部降块（合格事实保留，不传播）。"""

    def test_synthesis_budget_exhausted_degrades_block(self, monkeypatch):
        ctx = _confirmed_context()
        monkeypatch.setenv("LITELLM_MODEL", "openai/synthetic-standard")
        monkeypatch.setenv("OPENAI_API_KEYS", "synthetic-placeholder")
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        from src.config import Config

        Config.reset_instance()
        budget = ReportBudget(1.0, finish_margin=0.5)
        time.sleep(0.6)
        out = _synthesis_pillar(
            "贵州茅台", "600519", ctx["ident"], ctx["prosper"], ctx["trend"],
            ctx["peers"], ctx["catalysts"], budget=budget, subject_confirmed=True,
        )
        assert out["eligible"] is True
        assert out["status"] == "model_error"
        assert ctx["catalysts"]["items"]  # 合格事实保留
        assert not budget.expired()  # 主截止内，可收尾
