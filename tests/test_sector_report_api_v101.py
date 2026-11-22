# -*- coding: utf-8 -*-
"""板块申万研究闭环（issue #101 主测试缝）。

从公开板块 API 发起生成，经真实申万编排、既有行业催化检索（统一准入）与
证据化综合（登记表校验）逻辑、严格响应校验、临时 DB/文件、详情读回与
下载观察。测试内容：

- 完整资格报告闭环：沪深 included + 合格催化 + 综合ok → 完整性恢复
  complete；machine（载荷）/正文（Markdown）/详情读回/下载四方一致；
  可选概念失败不吃掉必需研究（complete 不受可选缺口影响）；零东财出站
  （东财链全部入口缝替换为即时失败哨兵）；
- 催化检索状态按实际检索成功区分：全部检索失败 / 检索成功零行与其他渠道
  失败并存（不误报全失败）/ 部分检索成功但无合格结果且其他供应商失败 /
  未发现合格催化（含全成功零行），诊断如实区分且不冒充「无新闻」；
- 参考日 + 成分未知：上一交易日参考行情不进入当日证据，综合证据携带
  真实日期，目标独立行情不进入同业证据；
- 选定口径后研究恰好一次（保留申万 / 东财切换 / 部分恢复）；
- 认证与路由隔离。

iFinD 标准 HTTP 传输与认证在模块缝替换（响应形状取自真实脱敏样本）；
搜索 provider / LLM 终端在既有受控缝替换（合成 wire，LiteLLM 流协议真实）；
严格交易日历用真实 exchange-calendars（时钟经既有注入缝冻结到已完成
会话盘后，任意墙钟时间确定性）；资格判定/严格校验/渲染/持久化不 mock。
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import patch

try:
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm  # noqa: F401
except ModuleNotFoundError:
    import sys
    from unittest.mock import MagicMock

    sys.modules["litellm"] = MagicMock()

from src.schemas.sector_analysis import SectorReportPayload
from src.search_service import SearchResult
from src.services import sector_sw_market as sw_mod

from tests.test_sector_report_api_v100 import (
    BJ_TARGET,
    BJ_TARGET_SEC,
    COAL_MEMBERS,
    SH_TARGET,
    SH_TARGET_SEC,
    SW31_CODES,
    SectorReportApiV100TestCase,
    _SwTransportStub,
)

# 冻结时刻：已完成会话盘后（任意墙钟时间下会话/资格判定确定性）
FROZEN_NOW = datetime(2026, 11, 18, 22, 30, 0)


def _install_api_synthetic_wire(content: str):
    """合成 LLM wire（httpx.AsyncClient 终端；放行 TestClient 的 ASGI 请求）。

    与 test_sector_synthesis_adapter 的受控 wire 同一终端缝（LiteLLM 流
    协议全链路真实），但仅拦截非 testserver 请求（即 LiteLLM 外部出站），
    避免把测试客户端自身的 ASGI 调用也拦截成 SSE。
    """
    import httpx

    chunks = [
        {"id": "s", "object": "chat.completion.chunk", "created": 0,
         "model": "synthetic-standard",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": content},
                      "finish_reason": None}]},
        {"id": "s", "object": "chat.completion.chunk", "created": 0,
         "model": "synthetic-standard",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    wire = "".join(
        "data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks
    ) + "data: [DONE]\n\n"
    orig_send = httpx.AsyncClient.send

    async def _synthetic_send(client_self, request, *args, **kwargs):
        if str(request.url).startswith("http://testserver"):
            return await orig_send(client_self, request, *args, **kwargs)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=wire.encode(), request=request)

    return patch.object(httpx.AsyncClient, "send", _synthetic_send)


_SYNO_CONTENT = json.dumps(
    {"claims": [{
        "kind": "interpretation",
        "text": "行业政策新闻构成板块催化，目标股处于成分成交活跃样本之内",
        "evidence_refs": ["cat-1", "identity"],
    }]},
    ensure_ascii=False,
)


class _FakeSearchProvider:
    """受控合成搜索 provider（终端响应隔离；search() 返回给定结果）。"""

    def __init__(self, name: str, *, rows: Optional[List[Dict[str, Any]]] = None,
                 success: bool = True, error_message: Optional[str] = None,
                 exc: Optional[Exception] = None) -> None:
        self.name = name
        self._rows = rows or []
        self._success = success
        self._error_message = error_message
        self._exc = exc
        self.calls: List[str] = []

    @property
    def is_available(self) -> bool:
        return True

    def search(self, query: str, max_results: int = 5, days: int = 7):
        self.calls.append(query)
        if self._exc is not None:
            raise self._exc
        results = [SearchResult(**row) for row in self._rows]
        return type("R", (), {
            "query": query, "results": results, "provider": self.name,
            "success": self._success, "error_message": self._error_message,
            "search_time": 0.01,
        })()


def _news_row(title: str, url: str, *, published: str,
              snippet: str = "行业政策与事件") -> Dict[str, Any]:
    return {"title": title, "snippet": snippet, "url": url, "source": "syn",
            "published_date": published}


def _frozen_sessions(count: int) -> List[str]:
    """冻结时刻下的最近 count 个已完成交易日（最新在前，真实严格日历）。"""
    from src.core.trading_calendar import recent_completed_trading_sessions

    sessions = recent_completed_trading_sessions("cn", count,
                                                 current_time=FROZEN_NOW)
    assert sessions is not None, "真实 XSHG 日历应可用（离线数据）"
    return [d.isoformat() for d in sessions]


def _frozen_session_date() -> str:
    return _frozen_sessions(1)[0]


class SectorReportApiV101TestCase(SectorReportApiV100TestCase):
    """v101 基座：合成 LLM wire 渠道隔离 + 场景显式时钟冻结。"""

    def setUp(self) -> None:
        super().setUp()
        # LLM 渠道不清洗：基座受控环境已提供合成模型配置；综合出站统一经
        # 合成 wire（httpx 终端）受控返回，任意渠道路由都被拦截（离线）。
        # 时钟不在此冻结：各场景经 self._freeze 显式冻结到自己的时刻
        # （叠加冻结会让后装 patcher 经已替换的 _session_context 解析出
        # 前一时刻的会话，见 _freeze_clocks 的真实上下文解析方式）。
        # `_freeze` 继承 v100 基座实现：它同时记录 `self._frozen_now`
        # （场景时钟一致源），`_install_sw_stub` 据此用同一冻结时刻安排
        # stub 会话/趋势日期——本地覆盖版曾漏记该字段，TZ≠本地（CI
        # UTC runner）时安排与请求会话漂移到不同交易日（东财哨兵兜底
        # 被 500 命中），这里不留第二个冻结入口。

    def _install_search_providers(self, *providers: _FakeSearchProvider) -> None:
        from src.search_service import get_search_service

        patcher = patch.object(get_search_service(), "_providers",
                               list(providers))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _qualified_sw_stub(self) -> _SwTransportStub:
        self._freeze(FROZEN_NOW)
        stub = _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=SH_TARGET_SEC, member_codes=COAL_MEMBERS)
        stub.session_date = _frozen_session_date()
        stub._history_dates = _frozen_sessions(20)
        return stub


class TestFullQualifyingReportLoop(SectorReportApiV101TestCase):
    """完整资格报告闭环：研究接通后完整性恢复 complete，四方一致。"""

    def test_full_loop_with_catalysts_and_synthesis_restores_complete(self):
        provider = _FakeSearchProvider("SyntheticSearch", rows=[
            _news_row("煤炭行业新政发布",
                      "https://news.example.com/coal/1.html",
                      published=datetime.now().date().isoformat()),
        ])
        stub = self._qualified_sw_stub()
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        self._install_search_providers(provider)
        wire = _install_api_synthetic_wire(_SYNO_CONTENT)
        with wire:
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        analysis = body["analysis"]
        validated = SectorReportPayload.model_validate(analysis)
        self.assertEqual(validated.payload_version, 3)
        # ---- 完整性恢复：confirmed + 当日目标口径行情 + 趋势 + 成分同业 +
        #      合格催化 + 综合 ok（无拒绝）→ complete ----
        assessment = analysis["assessment"]
        self.assertEqual(assessment["status"], "unscored")
        self.assertEqual(assessment["completeness"], "complete")

        # ---- 催化块：一次检索、统一准入、真实日期 ----
        cat = analysis["sector_catalysts"]
        self.assertIsNotNone(cat)
        self.assertTrue(cat["eligible"])
        self.assertEqual(cat["provider"], "SyntheticSearch")
        self.assertEqual(len(provider.calls), 1)  # 选定口径后检索恰好一次
        self.assertTrue(cat["items"])
        item = cat["items"][0]
        self.assertEqual(item["published_date"],
                         datetime.now().date().isoformat())
        self.assertEqual(item["date_basis"], "provider_published")
        self.assertIn("行业", cat["query"])

        # ---- 综合块：登记表校验 + 服务端事实摘要 ----
        syn = analysis["sector_synthesis"]
        self.assertIsNotNone(syn)
        self.assertEqual(syn["status"], "ok")
        self.assertEqual(syn["rejected_count"], 0)
        self.assertEqual(syn["summary_origin"], "server_facts")
        self.assertTrue(syn["claims"])
        self.assertTrue(syn["claims"][0]["evidence_refs"])
        # 服务端事实摘要从已采纳事实渲染（目标日行情/趋势/同业/目标股段落）
        self.assertIn("板块指数当日涨跌幅", syn["summary"])
        self.assertIn("同业样本", syn["summary"])
        self.assertIn("目标股", syn["summary"])

        # ---- machine（载荷）→ 正文（Markdown）一致 ----
        md = body["markdown"]
        self.assertNotIn("待后续版本接入", md)
        # 目标口径措辞保持既有格式（#102 复审 Spec 3：参考日措辞仅在
        # snapshot_mode=reference 启用，target/东财口径逐字不变）
        self.assertIn("## 二、当日板块行情", md)
        self.assertIn("当日涨跌幅", md.split("## 三、")[0])
        self.assertIn("## 四、行业催化", md)
        self.assertIn(item["title"], md)
        self.assertIn(item["published_date"], md)
        self.assertIn("## 六、证据化综合研判与风险", md)
        self.assertIn("服务端事实摘要", md)

        # ---- 存档一致：详情读回与下载 ----
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_analysis = detail.json()["data"]["analysis_json"]
        self.assertEqual(detail_analysis["assessment"]["completeness"],
                         "complete")
        self.assertEqual(detail_analysis["sector_catalysts"], cat)
        self.assertEqual(detail_analysis["sector_synthesis"], syn)
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.text, md)

        # ---- 零东财出站（哨兵即时失败）+ 研究零新增 iFinD 出站 ----
        # 31 快照 + L1 成员批次 = 2；#104 L2 批次为 0（issue #105 修复：
        # 二级成员行情按带交易所完整代码复用同报告一级已合格报价——二级
        # 名单与一级同名单时零二级行情批次出站）
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        self.assertEqual(len(rt_calls), 2)

    def test_optional_concepts_failure_never_starves_required_research(self):
        """可选概念失败（入口关闭）不影响必需研究完整取得与完整状态。"""
        provider = _FakeSearchProvider("SyntheticSearch", rows=[
            _news_row("煤炭行业新政发布",
                      "https://news.example.com/coal/2.html",
                      published=datetime.now().date().isoformat()),
        ])
        stub = self._qualified_sw_stub()
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        self._install_search_providers(provider)
        wire = _install_api_synthetic_wire(_SYNO_CONTENT)
        # ENABLE_IFIND_MCP=false（基座环境）：概念入口关闭 → 可选缺口
        with wire:
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        concepts = analysis["concepts"]
        self.assertEqual(concepts["items"], [])
        self.assertTrue(concepts["gaps"])
        gaps_text = " ".join(analysis["gaps"])
        self.assertIn("概念", gaps_text)
        # 必需研究照常完整取得；完整性恢复 complete（可选缺口由 gaps 承载）
        self.assertEqual(analysis["sector_catalysts"]["items"][0]["evidence_id"],
                         "cat-1")
        self.assertEqual(analysis["sector_synthesis"]["status"], "ok")
        self.assertEqual(analysis["assessment"]["completeness"], "complete")


class TestCatalystSearchOutcomeDistinction(SectorReportApiV101TestCase):
    """催化检索状态区分（issue #101 验收 1；#98 复审按实际检索成功区分）：
    全部失败 / 检索成功零行与其他渠道失败并存 / 部分成功无合格 + 其他渠道
    失败 / 未发现合格催化（含全成功零行），诊断如实区分且不冒充「无新闻」；
    综合按既有规则 no_qualified_facts，报告存档为部分（部分报告语义保留）。"""

    def _generate(self, *providers: _FakeSearchProvider,
                  probe_html: str = "") -> Dict[str, Any]:
        stub = self._qualified_sw_stub()
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        self._install_search_providers(*providers)
        if probe_html:
            # 无日期候选的文章探查终端隔离（签名 (evidence, failure, final_url)；
            # 提取逻辑真实，无网络出站）
            from src.services import sector_research

            def _fake_probe(url: str, budget: Any):
                return (sector_research.extract_article_date_evidence(
                    probe_html, url), None, url)

            patcher = patch.object(self._svc, "_probe_article_date_evidence",
                                   _fake_probe)
            patcher.start()
            self.addCleanup(patcher.stop)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["analysis"]

    def test_all_providers_failed_is_total_failure_not_no_news(self):
        import requests as _rq

        provider = _FakeSearchProvider("SyntheticSearch",
                                       exc=_rq.ConnectionError("boom"))
        analysis = self._generate(provider)
        cat = analysis["sector_catalysts"]
        self.assertTrue(cat["eligible"])  # 资格在：资料未取得与资格不足分开
        self.assertIsNone(cat["provider"])
        self.assertEqual(cat["items"], [])
        gaps = " ".join(cat["gaps"])
        self.assertIn("全部检索失败", gaps)
        self.assertIn("SyntheticSearch", gaps)  # sanitized 供应商名保留
        self.assertNotIn("均无可用发布日期", gaps)
        self.assertEqual(analysis["sector_synthesis"]["status"],
                         "no_qualified_facts")
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        reasons = " ".join(analysis["assessment"]["reasons"])
        self.assertIn("行业催化未取得", reasons)
        # 存档一致：缺口诊断随详情读回保留
        rid = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
        ).json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertIn("全部检索失败", " ".join(
            detail.json()["data"]["analysis_json"]["sector_catalysts"]["gaps"]))

    def test_partial_success_without_qualified_and_other_channel_failed(self):
        import requests as _rq

        failing = _FakeSearchProvider("FailingFirst",
                                      exc=_rq.ConnectionError("down"))
        no_qualify = _FakeSearchProvider("OfftopicOnly", rows=[
            _news_row("完全无关的标题", "https://unrelated.example.com/x.html",
                      published=datetime.now().date().isoformat(),
                      snippet="无关内容"),
        ])
        analysis = self._generate(failing, no_qualify)
        cat = analysis["sector_catalysts"]
        self.assertTrue(cat["eligible"])
        self.assertEqual(cat["provider"], "OfftopicOnly")  # 有供应商取得结果
        self.assertEqual(cat["items"], [])
        self.assertGreaterEqual(cat["dropped_offtopic"], 1)
        gaps = " ".join(cat["gaps"])
        self.assertIn("部分检索成功", gaps)
        self.assertIn("无合格", gaps)
        self.assertIn("失败", gaps)  # 其他渠道失败诊断并陈
        self.assertIn("FailingFirst", gaps)  # 失败供应商 sanitized 名保留
        self.assertNotIn("均无可用发布日期", gaps)
        self.assertEqual(analysis["sector_synthesis"]["status"],
                         "no_qualified_facts")
        self.assertEqual(analysis["assessment"]["completeness"], "partial")

    def test_clean_success_without_qualified_is_no_eligible_found(self):
        provider = _FakeSearchProvider("SyntheticSearch", rows=[
            # 相关但全部无发布日期 → 背景线索，非已证实催化
            _news_row("煤炭行业动态一则",
                      "https://news.example.com/coal/3.html",
                      published=""),
        ])
        analysis = self._generate(
            provider,
            probe_html=("<!DOCTYPE html><html><head><title>煤炭行业动态</title>"
                        "</head><body><p>正文</p></body></html>"))
        cat = analysis["sector_catalysts"]
        self.assertEqual(cat["provider"], "SyntheticSearch")
        self.assertEqual(cat["items"], [])
        self.assertTrue(cat["background_items"])  # 无日期条目仅背景
        gaps = " ".join(cat["gaps"])
        self.assertIn("未发现合格催化", gaps)
        self.assertIn("无可用发布日期", gaps)
        self.assertNotIn("全部检索失败", gaps)
        self.assertEqual(analysis["sector_synthesis"]["status"],
                         "no_qualified_facts")
        md = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
        ).json()["markdown"]
        self.assertIn("未发现合格催化", md)
        self.assertIn("背景线索", md)

    def test_all_success_empty_results_is_no_eligible_found_not_failure(self):
        """全部供应商检索成功但零行结果：未发现合格催化（不冒充全失败）。"""
        analysis = self._generate(_FakeSearchProvider("EmptyOk", rows=[]))
        cat = analysis["sector_catalysts"]
        self.assertTrue(cat["eligible"])
        self.assertIsNone(cat["provider"])
        self.assertEqual(cat["items"], [])
        gaps = " ".join(cat["gaps"])
        self.assertIn("未发现合格催化", gaps)
        self.assertIn("检索成功", gaps)
        self.assertNotIn("全部检索失败", gaps)
        self.assertNotIn("检索失败", gaps)
        self.assertEqual(analysis["sector_synthesis"]["status"],
                         "no_qualified_facts")
        self.assertEqual(analysis["assessment"]["completeness"], "partial")

    def test_success_empty_with_failing_provider_is_mixed_not_total_failure(self):
        """#98 复审反例：检索成功（零行）+ 其他渠道失败——按实际成功区分，
        不得误报「全部检索失败」；失败供应商名保留，诊断次序失败在前。"""
        import requests as _rq

        empty_ok = _FakeSearchProvider("EmptyOk", rows=[])
        failing = _FakeSearchProvider("FailingSecond",
                                      exc=_rq.ConnectionError("down"))
        analysis = self._generate(empty_ok, failing)
        cat = analysis["sector_catalysts"]
        self.assertTrue(cat["eligible"])
        self.assertIsNone(cat["provider"])  # 无供应商取得结果
        self.assertEqual(cat["items"], [])
        gaps = cat["gaps"]
        joined = " ".join(gaps)
        self.assertNotIn("全部检索失败", joined)
        self.assertIn("检索成功但未发现窗口内结果", joined)
        self.assertIn("1 个检索失败", joined)
        self.assertIn("FailingSecond", joined)  # sanitized 失败供应商名保留
        # 次序：实际失败诊断在前，混合结论在后（完整性原因取首个真实失败）
        self.assertLess(
            next(i for i, g in enumerate(gaps) if "FailingSecond" in g),
            next(i for i, g in enumerate(gaps)
                 if "检索成功但未发现窗口内结果" in g))
        self.assertEqual(analysis["sector_synthesis"]["status"],
                         "no_qualified_facts")
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        reasons = " ".join(analysis["assessment"]["reasons"])
        self.assertIn("行业催化未取得", reasons)


class _RefSnapshotStub(_SwTransportStub):
    """昨收上涨而当日行情缺失反例（issue #101 验收 2）。

    31 指数快照：目标指数只有**立即上一已完成交易日**盘后行（+1.2 上涨），
    其余指数为目标交易日盘中合格行；成员行情与目标独立行情为目标交易日
    盘中合格行（真实日期随 wire 行携带）。
    """

    def __init__(self, *, ref_date: str, target_day: str,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ref_date = ref_date
        self.target_day = target_day

    def __call__(self, url: str, headers: Dict[str, str],
                 body: Optional[bytes], *, label: str):
        from tests.test_sector_report_api_v100 import _quote_row, _quotes_payload

        assert body is not None
        parsed = json.loads(body.decode())
        if "real_time_quotation" in url:
            codes = parsed["codes"].split(",")
            if codes == [c for c, _ in sw_mod.SW_L1_DIRECTORY]:
                rows = []
                for c, _n in sw_mod.SW_L1_DIRECTORY:
                    if c == "801950.SL":
                        row = _quote_row(c, self.ref_date, 1.2)
                        row["time"] = [f"{self.ref_date} 22:00:46"]
                        row["table"]["tradeTime"] = ["22:00:46"]
                    else:
                        row = _quote_row(c, self.target_day, 0.5)
                        row["time"] = [f"{self.target_day} 10:29:00"]
                        row["table"]["tradeTime"] = ["10:29:00"]
                    rows.append(row)
                return 200, json.dumps(_quotes_payload(rows)).encode()
            if codes != [self.target_secucode]:
                # 成员批次：目标交易日盘中合格行（真实日期）
                rows = [_quote_row(c, self.target_day,
                                   self.member_changes.get(c, 1.0))
                        for c in codes]
                for row in rows:
                    row["time"] = [f"{self.target_day} 10:29:00"]
                    row["table"]["tradeTime"] = ["10:29:00"]
                return 200, json.dumps(_quotes_payload(rows)).encode()
            row = _quote_row(self.target_secucode, self.target_day,
                             self.target_change, 4.0e7)
            row["time"] = [f"{self.target_day} 10:29:00"]
            row["table"]["tradeTime"] = ["10:29:00"]
            return 200, json.dumps(_quotes_payload([row])).encode()
        return super().__call__(url, headers, body, label=label)


def _real_sessions(count: int, now: datetime) -> List[Any]:
    from src.core.trading_calendar import recent_completed_trading_sessions

    sessions = recent_completed_trading_sessions("cn", count, current_time=now)
    assert sessions is not None, "真实 XSHG 日历应可用（离线数据）"
    return list(sessions)


class TestReferenceDateEvidenceDiscipline(SectorReportApiV101TestCase):
    """参考日 + 成分未知（issue #101 验收 2）：昨收上涨不进入当日证据。

    盘中目标交易日目标指数合格行情缺失、上一已完成交易日上涨：报告展示
    参考口径并标部分；综合证据（登记表→服务端摘要）携带真实日期，参考日
    行情不表述为当日；同业/目标行情归属交易日独立呈现，未确认成分的目标
    独立事实不进入同业证据。
    """

    def _scenario(self, *, syno_content: str = _SYNO_CONTENT) -> Dict[str, Any]:
        from datetime import datetime as _dt

        now = _dt(2026, 11, 19, 10, 30, 0)
        sessions = [d.isoformat() for d in _real_sessions(25, now)]
        session_date = "2026-11-19"  # 冻结盘中当日
        ref_date = sessions[0]
        assert ref_date == "2026-11-18"
        # 冻结到盘中时刻（唯一冻结入口；基座 setUp 不冻结）
        self._freeze(now)
        stub = _RefSnapshotStub(
            ref_date=ref_date, target_day=session_date,
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=BJ_TARGET_SEC, member_codes=COAL_MEMBERS)
        stub.session_date = session_date  # 成分查询观察日 = 目标交易日
        stub._history_dates = sessions[:20]
        self._sw_post_patcher = patch.object(sw_mod, "_http_api_curl_post", stub)
        self._sw_token_patcher = patch.object(sw_mod, "_resolve_access_token",
                                              stub.resolve_token)
        self._sw_post_patcher.start()
        self._sw_token_patcher.start()
        self.addCleanup(self._sw_post_patcher.stop)
        self.addCleanup(self._sw_token_patcher.stop)
        self._install_eastmoney_sentinels()
        provider = _FakeSearchProvider("SyntheticSearch", rows=[
            _news_row("煤炭行业新政发布",
                      "https://news.example.com/coal/4.html",
                      published=datetime.now().date().isoformat()),
        ])
        self._install_search_providers(provider)
        wire = _install_api_synthetic_wire(syno_content)
        with wire:
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                f"?stock_code={BJ_TARGET}&stock_name=测试北交所标的"
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        return {"analysis": body["analysis"], "md": body["markdown"],
                "rid": body["report_id"], "ref_date": ref_date,
                "session_date": session_date}

    def test_reference_quote_never_enters_same_day_evidence(self):
        out = self._scenario()
        analysis, md = out["analysis"], out["md"]
        ref_date, session_date = out["ref_date"], out["session_date"]

        intraday = analysis["background"]["intraday"]
        self.assertTrue(intraday["sector_found"])
        self.assertEqual(intraday["snapshot_mode"], "reference")
        self.assertEqual(intraday["snapshot_date"], ref_date)
        self.assertEqual(intraday["row"]["change_pct"], 1.2)  # 昨收上涨

        # 完整性：参考口径 + 成分未知 → 部分（原因可回查）
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        reasons = " ".join(analysis["assessment"]["reasons"])
        self.assertIn("参考口径", reasons)
        self.assertIn("成分状态未知", reasons)

        # 综合证据（服务端事实摘要）：参考日行情按真实日期呈现，
        # 不冒充「当日」；同业/目标行情归属交易日独立呈现
        syn = analysis["sector_synthesis"]
        self.assertEqual(syn["status"], "ok")
        summary = syn["summary"] or ""
        self.assertIn(f"参考交易日（{ref_date}）", summary)
        self.assertNotIn("板块指数当日涨跌幅", summary)
        self.assertIn(f"同日行情 {session_date}", summary)  # 同业真实归属日
        self.assertIn(f"行情日（{session_date}）", summary)  # 目标独立事实
        self.assertIn("独立事实", summary)

        # 目标独立事实不进入同业证据：items 无目标，tiq 独立展示
        peers = analysis["sector_peers"]
        self.assertTrue(peers["industry_reference"])
        self.assertNotIn(BJ_TARGET, [i["code"] for i in peers["items"]])
        tiq = peers["target_independent_quote"]
        self.assertIsNotNone(tiq)
        self.assertEqual(tiq["as_of_date"], session_date)
        self.assertEqual(tiq["change_pct"], 1.5)

        # Markdown 与载荷一致：参考口径标注 + 独立事实 + 真实日期
        self.assertIn(f"行情日期：**{ref_date}**", md)
        self.assertIn("参考口径", md)
        self.assertIn("目标股独立行情", md)
        self.assertIn(f"行情日期：{session_date}", md)
        # 参考口径渲染纪律（#102 复审 Spec 3）：第二节标题/指标/口径说明按
        # snapshot_mode 统一为参考日措辞——上一日上涨不得出现在「当日」字段
        section2 = md.split("## 三、")[0]
        self.assertIn("## 二、参考交易日板块行情", section2)
        self.assertIn("参考交易日涨跌幅：1.2%", section2)
        self.assertIn("参考交易日成交额", section2)
        self.assertIn("参考交易日行情维度", section2)
        self.assertIn("以上是**参考交易日行情**", section2)
        self.assertNotIn("当日涨跌幅", section2)
        self.assertNotIn("当日成交额", section2)
        # 存档读回与下载一致（实际保存与下载的 Markdown 均为参考日措辞）
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{out['rid']}")
        detail_analysis = detail.json()["data"]["analysis_json"]
        self.assertEqual(
            detail_analysis["background"]["intraday"]["snapshot_date"],
            ref_date)
        self.assertEqual(detail_analysis["sector_synthesis"]["summary"],
                         syn["summary"])
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{out['rid']}/markdown?download=1")
        self.assertEqual(download.text, md)
        self.assertIn("## 二、参考交易日板块行情", download.text)

    def test_adversarial_constituent_and_reference_claims_are_rejected(self):
        """对抗性模型输出回归（#98 复审）：引用未确认成分的独立目标事实
        （target）与参考日行情（intraday）的结论——「目标股领涨指数成分
        样本」类成分内强度断言、跨会话拼接断言——在引用资格校验整条拒绝
        （detached ID = 未知引用，字段屏幕而非 prompt）；报告保持部分，
        无 500，被拒结论不进入载荷或存档。"""
        adversarial = json.dumps({"claims": [
            {"kind": "interpretation",
             "text": "目标股领涨指数成分样本，板块延续走强",
             "evidence_refs": ["target", "peers"]},
            {"kind": "risk",
             "text": "参考交易日板块上涨构成当日强度证据",
             "evidence_refs": ["intraday"]},
        ]}, ensure_ascii=False)
        out = self._scenario(syno_content=adversarial)
        analysis = out["analysis"]
        syn = analysis["sector_synthesis"]
        # 全部结论被拒：无合格产出（引用 detached ID = 未知引用）
        self.assertEqual(syn["status"], "malformed_output")
        self.assertEqual(syn["claims"], [])
        self.assertIn("unknown_evidence_ref", " ".join(syn["gaps"]))
        # 报告保持部分（参考口径 + 成分未知 + 综合未产出），存档一致
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        rid = out["rid"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_syn = detail.json()["data"]["analysis_json"]["sector_synthesis"]
        self.assertEqual(detail_syn["status"], "malformed_output")
        self.assertEqual(detail_syn["claims"], [])


class TestCalendarUnavailableTrendKeepsPartial(SectorReportApiV101TestCase):
    """#98 复审：confirmed + 严格 20 交易日日历不可用（趋势 eligible=False）
    时，其余证据健康的报告如实 partial——修复前该组合跳过趋势检查恢复
    complete，被 compose 完整性契约拒绝为 icontract ViolationError
    （表现为 API 500）。"""

    def test_trend_calendar_unavailable_keeps_partial_with_reason(self):
        provider = _FakeSearchProvider("SyntheticSearch", rows=[
            _news_row("煤炭行业新政发布",
                      "https://news.example.com/coal/7.html",
                      published=datetime.now().date().isoformat()),
        ])
        stub = self._qualified_sw_stub()
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        self._install_search_providers(provider)
        # 20 交易日窗口日历不可用（会话上下文/接收钟与其余窗口冻结保持不变）
        svc = self._svc
        frozen_sessions = svc.recent_completed_trading_sessions

        def _calendar_gap(market, count, *, current_time=None):
            if count >= 20:
                return []
            return frozen_sessions(market, count, current_time=current_time)

        gap_patcher = patch.object(svc, "recent_completed_trading_sessions",
                                   _calendar_gap)
        gap_patcher.start()
        self.addCleanup(gap_patcher.stop)
        wire = _install_api_synthetic_wire(_SYNO_CONTENT)
        with wire:
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                f"?stock_code={SH_TARGET}&stock_name=兖矿能源")
        self.assertEqual(resp.status_code, 200, resp.text)  # 不是 500
        analysis = resp.json()["analysis"]
        trend = analysis["sector_trend"]
        self.assertFalse(trend["eligible"])
        self.assertIsNone(trend["period_return_pct"])
        self.assertTrue(any("严格交易日历不可用" in g for g in trend["gaps"]))
        # 其余证据健康：催化合格、综合 ok（不受趋势缺口牵连）
        self.assertEqual(analysis["sector_catalysts"]["provider"],
                         "SyntheticSearch")
        self.assertTrue(analysis["sector_catalysts"]["items"])
        self.assertEqual(analysis["sector_synthesis"]["status"], "ok")
        # 完整性：如实 partial（原因=行业趋势未取得），原因可回查
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        reasons = " ".join(analysis["assessment"]["reasons"])
        self.assertIn("行业近期趋势未取得", reasons)
        self.assertIn("严格交易日历不可用", reasons)


class TestAuthAndRouteIsolation(SectorReportApiV101TestCase):
    """认证与路由隔离（issue #101 验收）：板块研究不改变共享路由/配置语义。

    - ADMIN_AUTH_ENABLED=true 时板块生成需要管理员会话（401 → 登录 → 200）；
    - 健康检查豁免与板块列表端点同样受保护（同一中间件语义）；
    - 板块研究出站后不向共享路由泄漏请求截止（ContextVar finally 复位），
      搜索 provider 注册表不变（测试缝恢复），东财缝零触碰（哨兵）。
    """

    def _enable_auth(self) -> None:
        env_path = self.data_dir / ".env"
        text = env_path.read_text(encoding="utf-8").replace(
            "ADMIN_AUTH_ENABLED=false", "ADMIN_AUTH_ENABLED=true")
        env_path.write_text(text, encoding="utf-8")
        import src.auth as auth_mod

        # is_auth_enabled 按请求时读取但缓存全局值；测试内重置缓存使
        # ENV_FILE 变更生效（既有 auth 中间件语义：注册后请求时判定）。
        patcher = patch.object(auth_mod, "_auth_enabled", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_generate_requires_admin_session_then_works_after_login(self):
        stub = self._qualified_sw_stub()
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        provider = _FakeSearchProvider("SyntheticSearch", rows=[
            _news_row("煤炭行业新政发布",
                      "https://news.example.com/coal/5.html",
                      published=datetime.now().date().isoformat()),
        ])
        self._install_search_providers(provider)
        self._enable_auth()
        # 未认证：生成与列表均 401；健康检查豁免
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源")
        self.assertEqual(resp.status_code, 401)
        listed = self.client.get("/api/v1/sector-analysis/reports")
        self.assertEqual(listed.status_code, 401)
        health = self.client.get("/api/v1/health")
        self.assertNotEqual(health.status_code, 401)
        # 首次登录设置密码 → 会话 Cookie → 生成可达（受控链下正常生成）
        login = self.client.post("/api/v1/auth/login", json={
            "password": "sector-v101-pass",
            "passwordConfirm": "sector-v101-pass",
        })
        self.assertEqual(login.status_code, 200, login.text)
        wire = _install_api_synthetic_wire(_SYNO_CONTENT)
        with wire:
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                f"?stock_code={SH_TARGET}&stock_name=兖矿能源")
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["assessment"]["completeness"], "complete")

    def test_research_outbound_leaks_no_deadline_and_keeps_providers(self):
        stub = self._qualified_sw_stub()
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        provider = _FakeSearchProvider("SyntheticSearch", rows=[
            _news_row("煤炭行业新政发布",
                      "https://news.example.com/coal/6.html",
                      published=datetime.now().date().isoformat()),
        ])
        self._install_search_providers(provider)
        from src.patches.eastmoney_patch import request_deadline
        from src.search_service import get_search_service

        providers_before = list(get_search_service()._providers)
        self.assertEqual(request_deadline.get(), None)
        wire = _install_api_synthetic_wire(_SYNO_CONTENT)
        with wire:
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                f"?stock_code={SH_TARGET}&stock_name=兖矿能源")
        self.assertEqual(resp.status_code, 200, resp.text)
        # 生成完成后：无泄漏的请求截止（线程内 bind / finally reset 语义），
        # provider 注册表身份不变（板块调用不改共享搜索配置）
        self.assertEqual(request_deadline.get(), None)
        self.assertEqual(get_search_service()._providers, providers_before)


class TestMissingTargetCoverageNoMarketEvidence(SectorReportApiV101TestCase):
    """目标指数无可用行情、其余 30 个指数有值（#102 复审 Spec 4，API/综合
    边界）：30/31 只是覆盖事实——报告保留覆盖说明，但**不给无数据目标注册
    可引用 intraday 市场证据或冒充目标快照**：服务端事实摘要不出现板块
    当日涨跌幅段落，引用 intraday 的模型结论在登记表引用校验被拒。"""

    def _scenario(self, syno_content: str) -> Dict[str, Any]:
        stub = self._qualified_sw_stub()
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        orig_call = stub.__call__

        def _missing_target_call(url, headers, body_bytes, *, label):
            parsed = json.loads(body_bytes.decode())
            if "real_time_quotation" in url \
                    and parsed.get("codes") == ",".join(SW31_CODES):
                payload = json.loads(orig_call(
                    url, headers, body_bytes, label=label)[1].decode())
                payload["tables"] = [
                    row for row in payload.get("tables") or []
                    if row.get("thscode") != "801950.SL"]
                return 200, json.dumps(payload).encode()
            return orig_call(url, headers, body_bytes, label=label)

        self._sw_post_patcher = patch.object(sw_mod, "_http_api_curl_post",
                                             _missing_target_call)
        self._sw_token_patcher = patch.object(
            sw_mod, "_resolve_access_token", stub.resolve_token)
        self._sw_post_patcher.start()
        self._sw_token_patcher.start()
        self.addCleanup(self._sw_post_patcher.stop)
        self.addCleanup(self._sw_token_patcher.stop)
        provider = _FakeSearchProvider("SyntheticSearch", rows=[
            _news_row("煤炭行业新政发布",
                      "https://news.example.com/coal/9.html",
                      published=datetime.now().date().isoformat()),
        ])
        self._install_search_providers(provider)
        wire = _install_api_synthetic_wire(syno_content)
        with wire:
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        return {"analysis": body["analysis"], "md": body["markdown"],
                "rid": body["report_id"],
                "session_date": stub.session_date}

    def test_coverage_only_report_has_no_citable_target_intraday(self):
        out = self._scenario(_SYNO_CONTENT)
        analysis, md = out["analysis"], out["md"]
        intraday = analysis["background"]["intraday"]
        # 无目标合格行（当日与参考均无）：不发布行情块——sector_found=false
        # 且快照元数据（日期/口径/来源）全空；不以 N/A 数值表冒充实际行情
        self.assertFalse(intraday["sector_found"])
        self.assertIsNone(intraday["snapshot_mode"])
        self.assertIsNone(intraday["snapshot_date"])
        self.assertIsNone(intraday["snapshot_source"])
        self.assertIsNone(intraday["row"].get("change_pct"))
        self.assertIsNone(intraday["momentum"])
        section2 = md.split("## 三、")[0]
        self.assertIn("当日行情未产出", section2)
        self.assertNotIn("当日行情维度", section2)   # 不渲染 N/A 数值表
        self.assertNotIn("当日涨跌幅：N/A", section2)
        # 精确 30/31 覆盖事实与明确的目标不足缺口在载荷缺口与实际存档
        # Markdown 中保留（缺口首位呈现）
        self.assertIn("其余 30/31 个指数取得当日合格行情",
                      " ".join(intraday["gaps"]))
        self.assertIn(f"目标指数 801950.SL 无 {out['session_date']} 同日合格行情",
                      intraday["gaps"][0])
        self.assertIn("其余 30/31 个指数取得当日合格行情", md)
        gaps = " ".join(analysis["gaps"])
        self.assertIn("其余 30/31 个指数取得当日合格行情", gaps)
        # 完整性如实部分（目标动态字段未取得），报告实际存档
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{out['rid']}")
        self.assertEqual(detail.status_code, 200)
        # 综合边界：无 citable intraday 证据 → 服务端事实摘要无板块当日
        # 涨跌幅段落（不冒充目标快照）；其余已取得事实照常渲染
        syn = analysis["sector_synthesis"]
        self.assertEqual(syn["status"], "ok")
        summary = syn["summary"] or ""
        self.assertNotIn("板块指数当日涨跌幅", summary)
        self.assertIn("板块区间收益", summary)

    def test_intraday_claim_rejected_without_target_quote(self):
        """引用 intraday 的模型结论被拒（unknown_evidence_ref）——目标无
        合格行情时登记表不含可引用市场证据，引用校验层切断而非 prompt。"""
        adversarial = json.dumps({"claims": [
            {"kind": "interpretation",
             "text": "板块当日上涨，动能延续",
             "evidence_refs": ["intraday"]},
        ]}, ensure_ascii=False)
        out = self._scenario(adversarial)
        syn = out["analysis"]["sector_synthesis"]
        self.assertEqual(syn["status"], "malformed_output")
        self.assertEqual(syn["claims"], [])
        self.assertIn("unknown_evidence_ref:intraday",
                      " ".join(syn["gaps"]))
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{out['rid']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["data"]["analysis_json"]["sector_synthesis"]["status"],
            "malformed_output")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
