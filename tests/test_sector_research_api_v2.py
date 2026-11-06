# -*- coding: utf-8 -*-
"""板块研究 #83 公开 API 全链路（继承 #82 API 基座）。

公开 POST → 真实身份/严格日历/趋势/同业/SW 解析 → 真实 SearchService
（合成 provider 终端）→ 真实统一准入 → 真实文章日期证据提取（合成 HTML，
终端探查隔离）→ 真实 LLMToolAdapter/LiteLLM 流（合成 SSE wire）→ 真实
登记表/引用与数字锚定校验 → strict → 临时 SQLite/文件 → list/detail/
download 一致。只隔离终端供应商/模型响应；准入、去重、登记表、校验、
渲染、存档全部真实。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from tests.test_sector_report_api_v2 import (
    SectorReportApiV2TestCase,
    _board_table,
    _daily_hist_df,
)


class SectorResearchApiV3TestCase(SectorReportApiV2TestCase):
    """#83 全链路基座：继承 #82 供应商隔离 + 合成研究终端注入。"""

    def _generate_confirmed(self, *, board_hist, bench_hist, cons_codes=None,
                            cons_df=None, calendar_sessions="real",
                            results: Optional[List[Dict[str, Any]]] = None,
                            model_claims: Optional[List[Dict[str, Any]]] = None,
                            install_research: bool = True):
        if install_research:
            self._install_research_success(results=results,
                                           model_claims=model_claims)
        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=cons_codes or ["600519", "000568"], cons_df=cons_df,
            board_table=boards, board_hist=board_hist, bench_hist=bench_hist,
            calendar_sessions=calendar_sessions,
        )
        return self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )

    def _real_window_hist(self):
        from src.core.trading_calendar import recent_completed_trading_sessions

        expected = sorted(recent_completed_trading_sessions("cn", 20))
        return (
            _daily_hist_df(expected, base=1000.0, step=1.0),
            _daily_hist_df(expected, base=4000.0, step=1.0,
                           date_col="date", close_col="close"),
        )


class TestFullLoopWithCatalystsAndSynthesis(SectorResearchApiV3TestCase):
    """完整正常报告：催化+综合真实取得并存档一致。"""

    def test_generate_detail_download_consistent_with_new_blocks(self):
        board_hist, bench_hist = self._real_window_hist()
        resp = self._generate_confirmed(board_hist=board_hist,
                                        bench_hist=bench_hist)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        # 新块取得
        catalysts = analysis["sector_catalysts"]
        self.assertTrue(catalysts["eligible"])
        self.assertEqual(len(catalysts["items"]), 1)
        item = catalysts["items"][0]
        self.assertEqual(item["evidence_id"], "cat-1")
        self.assertEqual(item["date_basis"], "provider_published")
        self.assertTrue(item["published_date"])
        synthesis = analysis["sector_synthesis"]
        self.assertEqual(synthesis["status"], "ok")
        self.assertTrue(synthesis["claims"])
        refs = synthesis["claims"][0]["evidence_refs"]
        self.assertIn("cat-1", refs)
        # completeness: 全部必需块取得 → complete
        self.assertEqual(analysis["assessment"]["completeness"], "complete")
        # 旧评分退役不变
        self.assertIsNone(analysis["sector_score"])
        self.assertIsNone(analysis["band"])
        # 生成 → 详情 → 下载一致（新块逐字段一致；下载=存档原文）
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_analysis = detail.json()["data"]["analysis_json"]
        self.assertEqual(detail_analysis["sector_catalysts"]["items"],
                         catalysts["items"])
        self.assertEqual(detail_analysis["sector_synthesis"]["claims"],
                         synthesis["claims"])
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        markdown = download.text
        # Markdown 渲染含催化与综合章节、证据 ID 可追溯
        self.assertIn("行业催化", markdown)
        self.assertIn("证据化综合研判", markdown)
        self.assertIn("cat-1", markdown)
        self.assertNotIn("sk-", markdown)  # 无凭据泄漏
        # 列表可见
        listing = self.client.get("/api/v1/sector-analysis/reports")
        self.assertEqual(listing.status_code, 200)
        self.assertTrue(listing.json()["total"] >= 1)

    def test_model_invented_numeric_rejected_partial_report(self):
        """模型发明数值：真实锚定校验拒绝 → malformed partial 存档，事实保留。"""
        board_hist, bench_hist = self._real_window_hist()
        invented = [{"kind": "interpretation",
                     "text": "板块区间收益99.9%遥遥领先大盘",
                     "evidence_refs": ["cat-1"]}]
        resp = self._generate_confirmed(
            board_hist=board_hist, bench_hist=bench_hist,
            model_claims=invented)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["sector_catalysts"]["items"].__len__(), 1)
        self.assertEqual(analysis["sector_synthesis"]["status"], "malformed_output")
        self.assertEqual(analysis["sector_synthesis"]["claims"], [])
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        reasons = analysis["assessment"]["reasons"]
        self.assertTrue(any("综合" in r for r in reasons))
        # 存档一致（partial 同样存档）
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_analysis = detail.json()["data"]["analysis_json"]
        self.assertEqual(detail_analysis["sector_synthesis"]["status"],
                         "malformed_output")


class TestMixedRejectionPartialContract(SectorResearchApiV3TestCase):
    """边界终态（issue #83）：混合输出（部分合格 + 部分被拒）→ partial。"""

    def test_mixed_claims_ok_with_rejected_count_partial(self):
        """合格结论保留 + rejected_count 显式 → completeness=partial + 原因。"""
        board_hist, bench_hist = self._real_window_hist()
        mixed = [
            {"kind": "interpretation",
             "text": "白酒行业新政策构成近期催化，目标股在同业样本中位置居前",
             "evidence_refs": ["cat-1", "peers"]},
            {"kind": "interpretation",
             "text": "板块区间收益99.9%遥遥领先大盘",
             "evidence_refs": ["cat-1"]},  # 数量字符 → 整条拒绝
        ]
        resp = self._generate_confirmed(board_hist=board_hist,
                                        bench_hist=bench_hist,
                                        model_claims=mixed)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        syn = analysis["sector_synthesis"]
        # status=ok：合格结论与全部服务端事实保留（不是 malformed）
        self.assertEqual(syn["status"], "ok")
        self.assertEqual(len(syn["claims"]), 1)
        self.assertEqual(syn["rejected_count"], 1)
        # 混合输出不呈现「全部合格研究」：partial + 显式 rejected-output 原因
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        reasons = analysis["assessment"]["reasons"]
        self.assertTrue(any("已拒绝的结论输出" in r and "完整性不为完整" in r
                            for r in reasons), reasons)
        # 全部合格对照：rejected_count 缺省 0、completeness=complete 不受影响
        resp2 = self._generate_confirmed(board_hist=board_hist,
                                         bench_hist=bench_hist)
        self.assertEqual(resp2.status_code, 200, resp2.text)
        analysis2 = resp2.json()["analysis"]
        self.assertEqual(analysis2["sector_synthesis"]["rejected_count"], 0)
        self.assertEqual(analysis2["assessment"]["completeness"], "complete")
        # 生成 → 详情 → 下载一致：mixed 报告计数与完整性逐字段一致，
        # Markdown 显式拒绝计数行可见
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_analysis = detail.json()["data"]["analysis_json"]
        self.assertEqual(detail_analysis["sector_synthesis"]["rejected_count"], 1)
        self.assertEqual(detail_analysis["assessment"]["completeness"],
                         "partial")
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        markdown = download.text
        self.assertIn("条结论未通过定性/引用/结构校验并整条拒绝", markdown)
        self.assertIn("完整性为**部分**", markdown)
        # 历史列表可见（mixed partial 同样存档）
        listing = self.client.get("/api/v1/sector-analysis/reports")
        self.assertEqual(listing.status_code, 200)
        self.assertTrue(listing.json()["total"] >= 1)

    def test_all_invalid_claims_stay_malformed_partial(self):
        """全部结论被拒 → malformed_output/partial（保持既有语义）。"""
        board_hist, bench_hist = self._real_window_hist()
        invalid = [{"kind": "interpretation",
                    "text": "板块区间收益99.9%遥遥领先",
                    "evidence_refs": ["cat-1"]}]
        resp = self._generate_confirmed(board_hist=board_hist,
                                        bench_hist=bench_hist,
                                        model_claims=invalid)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["sector_synthesis"]["status"],
                         "malformed_output")
        self.assertEqual(analysis["sector_synthesis"]["rejected_count"], 0)
        self.assertEqual(analysis["assessment"]["completeness"], "partial")


class TestTerminalProgramErrorContract(SectorResearchApiV3TestCase):
    """边界终态（issue #83）：真实 SDK 包装下的终端程序错误诚实失败。"""

    def test_terminal_typeerror_via_sdk_wrap_yields_500_no_archive(self):
        """httpx 终端 TypeError 经真实 LiteLLM InternalServerError 包装 →
        opt-in 遍历真实异常对象链 rethrow → API 500、零存档。"""
        import httpx as _httpx

        orig_send = _httpx.AsyncClient.send

        async def _terminal_typeerror(client_self, request, *args, **kwargs):
            if isinstance(client_self._transport, _httpx.ASGITransport):
                return await orig_send(client_self, request, *args, **kwargs)
            raise TypeError("httpx.AsyncClient.send terminal programming error")

        board_hist, bench_hist = self._real_window_hist()
        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"], board_table=boards,
            board_hist=board_hist, bench_hist=bench_hist,
        )
        self._install_research_success()
        before_total = self.client.get(
            "/api/v1/sector-analysis/reports").json()["total"]
        # 换回真实合成 provider 的 wire 之外：模型出站抛 TypeError
        self._model_send_patcher.stop()
        with patch.object(_httpx.AsyncClient, "send", _terminal_typeerror):
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                "?stock_code=600519&stock_name=贵州茅台"
            )
        self.assertEqual(resp.status_code, 500, resp.text)
        body = resp.json()
        # 全局异常 handler 形状（root minor-review-closure 契约）：500 固定
        # 安全文案——响应不携带底层程序错误原文（rethrow 的 TypeError 原文
        # 与「All LLM models failed」包装都不出现在响应），诚实失败语义由
        # 500 状态 + 零存档承载；服务端日志仅异常类型 + 不含值位置
        # （filename/lineno/function），无参数/源码行/locals
        self.assertEqual(body.get("message") or "",
                         "生成失败：内部错误（详见服务端日志）")
        self.assertNotIn("terminal programming error", body.get("message") or "")
        self.assertNotIn("All LLM models failed", body.get("message") or "")
        after_total = self.client.get(
            "/api/v1/sector-analysis/reports").json()["total"]
        self.assertEqual(after_total, before_total)

    def test_remote_service_error_stays_model_error_partial_archive(self):
        """真·远端服务失败（消息文本提 TypeError，链上无真实对象）→
        opt-in 不误判 → model_error partial 正常存档（fallback 语义不变）。"""
        import httpx as _httpx
        import litellm

        orig_send = _httpx.AsyncClient.send
        remote = litellm.exceptions.InternalServerError(
            message='upstream 500: {"error": "TypeError in remote handler"}',
            llm_provider="openai", model="synthetic-standard",
        )

        async def _remote_failure(client_self, request, *args, **kwargs):
            if isinstance(client_self._transport, _httpx.ASGITransport):
                return await orig_send(client_self, request, *args, **kwargs)
            raise remote

        board_hist, bench_hist = self._real_window_hist()
        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"], board_table=boards,
            board_hist=board_hist, bench_hist=bench_hist,
        )
        self._install_research_success()
        self._model_send_patcher.stop()
        with patch.object(_httpx.AsyncClient, "send", _remote_failure):
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                "?stock_code=600519&stock_name=贵州茅台"
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["sector_synthesis"]["status"], "model_error")
        self.assertTrue(analysis["sector_synthesis"]["gaps"])
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        # partial 正常存档
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["data"]["analysis_json"]["sector_synthesis"]["status"],
            "model_error")


class TestTitleDayPrecisionArchive(SectorResearchApiV3TestCase):
    """边界终态（issue #83）：标题声明日参与判定，两日期路由 + 真实存档。"""

    def _admitted(self, title: str, *, published: Optional[str],
                  probe_html: Optional[str] = None):
        from src.services import sector_analysis_service as _svc
        from src.services import sector_research as _sr

        if probe_html is not None:
            def _fake_probe(url: str, budget):
                return (_sr.extract_article_date_evidence(probe_html, url),
                        None, url)

            probe_patch = patch.object(_svc, "_probe_article_date_evidence",
                                       _fake_probe)
            probe_patch.start()
            self.addCleanup(probe_patch.stop)
        results = None
        if published is not None:
            results = [{
                "title": title, "snippet": "监管部门发布白酒行业新规",
                "url": "https://example.com/news/title-day.html",
                "source": "example.com", "published_date": published,
            }]
        else:
            results = [{
                "title": title, "snippet": "监管部门发布白酒行业新规",
                "url": "https://example.com/news/title-day.html",
                "source": "example.com", "published_date": None,
            }]
        self._install_research_success(results=results)
        board_hist, bench_hist = self._real_window_hist()
        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"], board_table=boards,
            board_hist=board_hist, bench_hist=bench_hist,
        )
        return self.client.post(
            "/api/v1/sector-analysis/generate"
            "?stock_code=600519&stock_name=贵州茅台"
        )

    def test_known_past_declared_day_background_both_routes_archived(self):
        """标题声明 11-01（早于窗口起始）+ 发布/元数据日期今天 → 两路由
        均背景降级，partial 存档与下载一致；主题不再冒充近期催化。"""
        from datetime import datetime

        today = datetime.now().date().isoformat()
        title = "白酒行业2026年11月1日政策回顾"
        # provider-date 路由
        resp = self._admitted(title, published=today)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        cat = analysis["sector_catalysts"]
        self.assertEqual(cat["items"], [])
        self.assertEqual(len(cat["background_items"]), 1)
        self.assertIn("历史回顾", cat["background_items"][0]["downgrade_reason"])
        self.assertEqual(analysis["sector_synthesis"]["status"],
                         "no_qualified_facts")
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_cat = detail.json()["data"]["analysis_json"]["sector_catalysts"]
        self.assertEqual(detail_cat["background_items"],
                         cat["background_items"])
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertIn("历史回顾属性，仅背景", download.text)
        # article-metadata 路由（同标题 + 今日元数据确证 → 升级重入资格）
        resp2 = self._admitted(
            title, published=None,
            probe_html=(
                '<!DOCTYPE html><html><head>'
                f'<meta property="article:published_time" content="{today}'
                'T09:00:00">'
                "</head><body><p>正文</p></body></html>"
            ),
        )
        self.assertEqual(resp2.status_code, 200, resp2.text)
        cat2 = resp2.json()["analysis"]["sector_catalysts"]
        self.assertEqual(cat2["items"], [])
        self.assertEqual(len(cat2["background_items"]), 1)
        self.assertIn("历史回顾", cat2["background_items"][0]["downgrade_reason"])

    def test_within_window_declared_day_catalyst_archived(self):
        """窗口内/同日完整日期声明 → 保持已证实催化并存档（正控）。"""
        from datetime import datetime, timedelta

        now = datetime.now()
        y, m = now.year, now.month
        yesterday = (now - timedelta(days=1))
        title = (f"白酒行业{y}年{m}月{yesterday.day}日政策发布会召开")
        resp = self._admitted(title, published=now.date().isoformat())
        self.assertEqual(resp.status_code, 200, resp.text)
        cat = resp.json()["analysis"]["sector_catalysts"]
        self.assertEqual(len(cat["items"]), 1)
        self.assertEqual(cat["items"][0]["date_basis"], "provider_published")
        self.assertEqual(cat["background_items"], [])
        rid = resp.json()["report_id"]
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertIn("供应商发布字段", download.text)


class TestNoNewsSemantics(SectorResearchApiV3TestCase):
    """无可靠新闻 / 全部无日期：partial 准确标记，不冒充完整。"""

    def test_real_no_news_partial_without_model_outbound(self):
        board_hist, bench_hist = self._real_window_hist()
        wire_calls: List[str] = []

        import httpx as _httpx

        orig_send = _httpx.AsyncClient.send

        async def _counting_send(client_self, request, *args, **kwargs):
            if not isinstance(client_self._transport, _httpx.ASGITransport):
                wire_calls.append(str(request.url))
            return await orig_send(client_self, request, *args, **kwargs)

        from src.search_service import get_search_service

        class _EmptyProvider:
            name = "SyntheticEmpty"

            @property
            def is_available(self) -> bool:
                return True

            def search(self, query: str, max_results: int = 5, days: int = 7):
                return type("R", (), {
                    "query": query, "results": [], "provider": self.name,
                    "success": True, "error_message": None, "search_time": 0.01,
                })()

        search_svc = get_search_service()
        self._orig_providers = search_svc._providers
        search_svc._providers = [_EmptyProvider()]
        self.addCleanup(setattr, search_svc, "_providers", self._orig_providers)

        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"], board_table=boards,
            board_hist=board_hist, bench_hist=bench_hist,
        )
        with patch("httpx.AsyncClient.send", _counting_send):
            resp = self.client.post(
                "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        catalysts = analysis["sector_catalysts"]
        self.assertEqual(catalysts["items"], [])
        # 真实空结果 = 窗口内无可用新闻（不冒充供应商失败）
        self.assertTrue(any("窗口内无可用新闻" in g for g in catalysts["gaps"]))
        self.assertEqual(analysis["sector_synthesis"]["status"], "no_qualified_facts")
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        # 模型零出站（无合格催化不调用模型——进程内无其他模型调用）
        self.assertEqual(wire_calls, [])

    def test_provider_403_with_bearer_sanitized_end_to_end(self):
        """供应商 403 + synthetic bearer：缺口 sanitized，Markdown/下载无泄漏。"""
        board_hist, bench_hist = self._real_window_hist()
        from src.search_service import get_search_service

        leak = 'HTTP 403: denied "Authorization: Bearer sk-supersecret123"'

        class _FailProvider:
            name = "SyntheticFail"

            @property
            def is_available(self) -> bool:
                return True

            def search(self, query: str, max_results: int = 5, days: int = 7):
                return type("R", (), {
                    "query": query, "results": [], "provider": self.name,
                    "success": False, "error_message": leak, "search_time": 0.01,
                })()

        search_svc = get_search_service()
        self._orig_providers = search_svc._providers
        search_svc._providers = [_FailProvider()]
        self.addCleanup(setattr, search_svc, "_providers", self._orig_providers)

        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"], board_table=boards,
            board_hist=board_hist, bench_hist=bench_hist,
        )
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        payload_text = json.dumps(resp.json(), ensure_ascii=False)
        self.assertNotIn("sk-supersecret123", payload_text)
        self.assertNotIn("Bearer sk-supersecret123", payload_text)
        catalysts = resp.json()["analysis"]["sector_catalysts"]
        self.assertTrue(any("返回失败" in g for g in catalysts["gaps"]))
        self.assertEqual(resp.json()["analysis"]["sector_synthesis"]["status"],
                         "no_qualified_facts")
        # 存档下载同样无泄漏
        rid = resp.json()["report_id"]
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertNotIn("sk-supersecret123", download.text)


class TestUnconfirmedIdentityNoOutbound(SectorResearchApiV3TestCase):
    """unverified/ambiguous 身份：不搜索、不综合（零研究出站）。"""

    def test_unverified_membership_no_research_outbound(self):
        board_hist, bench_hist = self._real_window_hist()
        from src.search_service import SearchResult, get_search_service

        search_calls: List[str] = []

        class _CountingProvider:
            name = "SyntheticCounting"

            @property
            def is_available(self) -> bool:
                return True

            def search(self, query: str, max_results: int = 5, days: int = 7):
                search_calls.append(query)
                return type("R", (), {
                    "query": query, "results": [], "provider": self.name,
                    "success": True, "error_message": None, "search_time": 0.01,
                })()

        search_svc = get_search_service()
        self._orig_providers = search_svc._providers
        search_svc._providers = [_CountingProvider()]
        self.addCleanup(setattr, search_svc, "_providers", self._orig_providers)

        # 成分不含目标股 → unverified（板块锁定但归属未确认）
        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["000568"],  # 不含 600519
            board_table=boards, board_hist=board_hist, bench_hist=bench_hist,
        )
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["identity"]["subject_status"], "unverified")
        catalysts = analysis["sector_catalysts"]
        self.assertFalse(catalysts["eligible"])
        self.assertEqual(search_calls, [])  # 零搜索出站
        synthesis = analysis["sector_synthesis"]
        self.assertFalse(synthesis["eligible"])
        self.assertEqual(synthesis["status"], "skipped_identity")
