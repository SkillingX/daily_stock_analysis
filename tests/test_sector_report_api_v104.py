# -*- coding: utf-8 -*-
"""二级同业与全成分成交额名次 API 闭环（issue #104）。

基座复用 v101（临时 DB/报告目录 + 申万模块缝替身 + 东财哨兵 + 搜索替身 +
合成 LLM wire）；stub 扩展 ``ths_constituent_num_index`` 计数分支（basic_
data_service 按 indicator 路由——行业归属请求 indiparams[0] 是层级、计数
请求是 YYYYMMDD）。场景：干净双接口（成分名单 = 计数 = 成员行情齐）下从
公开 API 生成报告，验证生成响应、详情读回与 Markdown 下载对同一存档载荷
的 wire 值一致（生成 → 持久化 → 读回 trace）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from tests.test_sector_report_api_v100 import (
    COAL_MEMBERS,
    SH_TARGET,
    SH_TARGET_SEC,
    _SwTransportStub,
)
from tests.test_sector_report_api_v101 import (
    FROZEN_NOW,
    SectorReportApiV101TestCase,
)


class _CountAwareSwTransportStub(_SwTransportStub):
    """v100 stub 扩展：``ths_constituent_num_index`` 计数分支（#104）。

    计数请求（codes 为单指数、indiparams[0] 为 YYYYMMDD）返回与 L1/L2 成分
    名单一致的计数（干净双接口正例）；行业归属请求照旧按层级路由。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.count_calls: List[Tuple[str, str]] = []

    def __call__(self, url: str, headers: Dict[str, str],
                 body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
        assert body is not None
        parsed = json.loads(body.decode())
        if ("basic_data_service" in url
                and parsed.get("indipara")
                and parsed["indipara"][0].get("indicator")
                == "ths_constituent_num_index"):
            codes = parsed["codes"]
            ymd = parsed["indipara"][0]["indiparams"][0]
            self.count_calls.append((codes, ymd))
            payload = {
                "errorcode": 0, "errmsg": "",
                "tables": [{
                    "thscode": codes,
                    "table": {"ths_constituent_num_index":
                              [len(self.member_codes)]},
                }],
                "datatype": [{"itemid": "ths_constituent_num_index",
                              "type": "DT_INTEGER"}],
                "inputParams": {"jsonrpc": True, "params": [{
                    "function": "ths_constituent_num_index", "id": "1",
                    "params": [
                        {"name": "THSCODE", "system": "false", "value": ""},
                        {"name": "FDATE", "system": "false", "value": ymd},
                        {"name": "FDIR", "system": "false", "value": ""},
                    ],
                }]},
                "dataVol": 1,
            }
            return 200, json.dumps(payload).encode()
        return super().__call__(url, headers, body, label=label)


class TestL2PeersApiLoop(SectorReportApiV101TestCase):
    """生成 → 详情读回 → Markdown 下载的存档一致性（同一 wire 值 trace）。"""

    def _generate(self) -> Dict[str, Any]:
        from tests.test_sector_report_api_v101 import (
            _FakeSearchProvider,
            _install_api_synthetic_wire,
            _news_row,
            _SYNO_CONTENT,
        )

        provider = _FakeSearchProvider(name="p1", rows=[_news_row(
            "煤炭行业近期事件（受控）", "https://example.com/a",
            published="2026-11-12")])
        self._install_search_providers(provider)
        wire = _install_api_synthetic_wire(_SYNO_CONTENT)
        self.addCleanup(wire.stop)
        stub = _CountAwareSwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=SH_TARGET_SEC, member_codes=COAL_MEMBERS)
        self._freeze(FROZEN_NOW)
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertGreaterEqual(len(stub.count_calls), 1)
        # 计数请求单指数绑定 + 显式交易日（YYYYMMDD）
        codes_seen = {c for c, _ymd in stub.count_calls}
        self.assertTrue(all(len(c.split(",")) == 1 for c in codes_seen))
        self.assertTrue(all(
            ymd.isdigit() and len(ymd) == 8 for _c, ymd in stub.count_calls))
        return resp.json()

    def test_generate_detail_and_markdown_trace_same_values(self) -> None:
        data = self._generate()
        analysis = data["analysis"]
        l2 = analysis["sector_l2_peers"]
        self.assertIsNotNone(l2)
        # 干净双接口（名单 = 计数 = 报价全集）→ 全成分范围名次
        self.assertEqual(l2["amount_rank_scope"], "full_constituents")
        self.assertIsNotNone(l2["target_amount_rank"])
        self.assertEqual(l2["amount_rank_denominator"], len(COAL_MEMBERS))
        self.assertIn("双接口", l2["amount_rank_evidence"] or "")
        # 严格载荷契约真实执行（生成响应即存档形状）
        from src.schemas.sector_analysis import SectorReportPayload

        SectorReportPayload.model_validate(analysis)
        # 详情读回 = 生成响应的同一块（持久化 → 读回 trace）
        rid = data["report_id"]
        detail = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200, detail.text)
        detail_l2 = detail.json()["data"]["analysis_json"]["sector_l2_peers"]
        self.assertEqual(detail_l2, l2)
        self.assertEqual(
            detail_l2["target_amount_rank"], l2["target_amount_rank"])
        # Markdown 下载含同值名次与核验（Web/Markdown 同源存档）
        md = data["markdown"]
        self.assertIn("二级同业对照", md)
        self.assertIn(f"第 **{l2['target_amount_rank']}** / "
                      f"{l2['amount_rank_denominator']}", md)
        self.assertIn("全成分核验", md)
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.text, md)
