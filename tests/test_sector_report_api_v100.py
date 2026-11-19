# -*- coding: utf-8 -*-
"""板块申万成分边界公开 API 闭环（issue #100 主测试缝）。

从公开板块 API 发起生成，经真实申万编排/成分资格/同业选取逻辑、严格
响应校验、临时 DB/文件、详情读回与下载观察。测试内容：

- 北交所成分边界（unknown）：目标股带交易所身份未在同日成分集合中、
  响应无完整性元数据 → 成分状态未知；行业行情/趋势保留，同业标注行业
  参考，目标股独立行情独立展示（不进 items、不参与排名），成分内名次
  留空，报告为部分并如实标注原因；**成分差异本身零东财出站**（东财链
  全部入口缝替换为即时失败哨兵）；
- 沪深 included：目标股并入成交额前五样本、样本内名次可判、membership
  verified（sw_index_members）；零东财出站；
- 成分查询供应商失败：报告仍成功（部分），membership 保守未知 + 缺口；
- 页面/详情/下载同一载荷一致。

iFinD 标准 HTTP 传输与认证在模块缝替换（响应形状取自真实脱敏样本）；
严格交易日历用真实 exchange-calendars；资格判定/严格校验/渲染/持久化
不 mock。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

try:
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm  # noqa: F401
except ModuleNotFoundError:
    import sys
    from unittest.mock import MagicMock

    sys.modules["litellm"] = MagicMock()

from fastapi.testclient import TestClient

from api.app import create_app
from src.config import Config
from src.core.trading_calendar import recent_completed_trading_sessions
from src.schemas.sector_analysis import SectorReportPayload
from src.services import sector_sw_market as sw
from src.storage import DatabaseManager
from tests.test_sector_report_api_v99 import (
    FROZEN_COMPLETED_NOW,
    _freeze_clocks,
)

BJ_TARGET = "920101"
BJ_TARGET_SEC = "920101.BJ"
SH_TARGET = "600188"
SH_TARGET_SEC = "600188.SH"


def _history_payload(codes: List[str], dates: List[str]) -> Dict[str, Any]:
    tables = []
    for idx, code in enumerate(codes):
        base = 2000.0 + idx * 500.0
        tables.append({
            "thscode": code, "time": list(dates),
            "table": {"close": [base + i for i in range(len(dates))]},
        })
    return {"errorcode": 0, "errmsg": "", "tables": tables}


def _quotes_payload(quotes: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"errorcode": 0, "errmsg": "Success!", "tables": quotes}


def _quote_row(thscode: str, date: str, change: Optional[float],
               amount: float = 1.0e9) -> Dict[str, Any]:
    latest = (round(100.0 * (1 + change / 100.0), 3)
              if change is not None else None)
    return {
        "marketCategory": "1", "pricetype": 1, "thscode": thscode,
        "time": [f"{date} 22:00:46"],
        "table": {
            "changeRatio": [change], "amount": [amount],
            "tradeTime": ["22:00:46"], "latest": [latest], "preClose": [100.0],
            "turnoverRatio": [2.0], "riseCount": [20.0], "fallCount": [10.0],
        },
    }


def _basic_payload(thscode: str, name: str) -> Dict[str, Any]:
    return {
        "errorcode": 0, "errmsg": "",
        "tables": [{"thscode": thscode, "time": [],
                    "table": {"ths_the_sw_industry_stock": [name]}}],
    }


def _members_payload(session_date: str, codes: List[str]) -> Dict[str, Any]:
    return {"errorcode": 0, "errmsg": "", "tables": [{
        "table": {
            "p03473_f001": [session_date] * len(codes),
            "p03473_f002": list(codes),
            "p03473_f003": [f"成员{c[:6]}" for c in codes],
        },
    }]}


class _SwTransportStub:
    """申万模块缝替身：按端点与请求 codes 路由，记录全部请求。"""

    def __init__(self, *, industry_by_level: Dict[str, str],
                 target_secucode: str, member_codes: List[str],
                 members_error: Optional[Dict[str, Any]] = None,
                 member_changes: Optional[Dict[str, Optional[float]]] = None,
                 member_amounts: Optional[Dict[str, float]] = None,
                 target_independent: bool = True,
                 target_change: float = 1.5) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.industry_by_level = industry_by_level
        self.target_secucode = target_secucode
        self.member_codes = list(member_codes)
        self.members_error = members_error
        self.member_changes = member_changes or {}
        self.member_amounts = member_amounts or {}
        self.target_independent = target_independent
        self.target_change = target_change
        self.quotes_error = False
        self.session_date = ""

    def __call__(self, url: str, headers: Dict[str, str],
                 body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
        assert body is not None
        parsed = json.loads(body.decode())
        self.calls.append((url, parsed))
        if "basic_data_service" in url:
            level = parsed["indipara"][0]["indiparams"][0]
            return 200, json.dumps(_basic_payload(
                parsed["codes"], self.industry_by_level[level])).encode()
        if "data_pool" in url:
            if self.members_error is not None:
                return 200, json.dumps(self.members_error).encode()
            return 200, json.dumps(_members_payload(
                self.session_date, self.member_codes)).encode()
        if "real_time_quotation" in url:
            if self.quotes_error:
                return 200, json.dumps({"errorcode": -3, "errmsg": "timeout",
                                        "tables": []}).encode()
            codes = parsed["codes"].split(",")
            if codes == [c for c, _ in sw.SW_L1_DIRECTORY]:
                rows = [_quote_row(c, self.session_date,
                                   -0.87 if c == "801950.SL" else 0.5)
                        for c in SW31_CODES]
                return 200, json.dumps(_quotes_payload(rows)).encode()
            if codes == [self.target_secucode]:
                if not self.target_independent:
                    raise AssertionError(
                        "未确认成分前不应发起目标独立行情出站")
                return 200, json.dumps(_quotes_payload([
                    _quote_row(self.target_secucode, self.session_date,
                               self.target_change, 4.0e7)])).encode()
            rows = [_quote_row(c, self.session_date,
                               self.member_changes.get(c, 1.0),
                               self.member_amounts.get(c, 1.0e9))
                    for c in codes]
            return 200, json.dumps(_quotes_payload(rows)).encode()
        if "cmd_history_quotation" in url:
            codes = parsed["codes"].split(",")
            return 200, json.dumps(_history_payload(
                codes, self.history_dates)).encode()
        raise AssertionError(f"未预置的申万端点: {url}")

    @property
    def history_dates(self) -> List[str]:
        return self._history_dates

    @staticmethod
    def resolve_token(refresh_token: str, *, force_refresh: bool = False) -> str:
        return "access-token-stub"


SW31_CODES = [c for c, _ in sw.SW_L1_DIRECTORY]

COAL_MEMBERS = ["000552.SZ", "000723.SZ", "600188.SH", "601225.SH",
                "601898.SH", "002128.SZ"]


class SectorReportApiV100TestCase(unittest.TestCase):
    """临时 DB/报告目录 + 申万模块缝替身 + 东财哨兵的公开 API 测试基座。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "sector_api_v100_test.db"
        env_path = self.data_dir / ".env"
        env_path.write_text(
            "STOCK_LIST=600188\n"
            "ADMIN_AUTH_ENABLED=false\n"
            f"DATABASE_PATH={self.db_path}\n"
            "ENABLE_IFIND_MCP=false\n"
            "FINANCIAL_PRIMARY_SOURCE=choice\n"
            "DATA_SOURCE_PROFILE=choice\n"
            "SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS=300\n",
            encoding="utf-8",
        )
        self._env_backup: Dict[str, Optional[str]] = {}
        self._controlled_env = {
            "ENV_FILE": str(env_path),
            "DATABASE_PATH": str(self.db_path),
            "ADMIN_AUTH_ENABLED": "false",
            "ENABLE_IFIND_MCP": "false",
            "FINANCIAL_PRIMARY_SOURCE": "choice",
            "DATA_SOURCE_PROFILE": "choice",
            "SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS": "300",
            "IFIND_HTTP_REFRESH_TOKEN": "sector-test-refresh-token",
            "IFIND_HTTP_TIMEOUT_SECONDS": "10",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "LITELLM_MODEL": "openai/synthetic-standard",
            "OPENAI_API_KEYS": "synthetic-placeholder",
        }
        for name, value in self._controlled_env.items():
            self._env_backup[name] = os.environ.get(name)
            os.environ[name] = value
        Config.reset_instance()
        DatabaseManager.reset_instance()
        orig_connect = socket.socket.connect

        def _guarded_connect(sock, address):  # noqa: ANN001 - socket 签名透传
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError(f"离线回归意外出网: {address!r}")
            return orig_connect(sock, address)

        self._network_guard = patch.object(socket.socket, "connect",
                                           _guarded_connect)
        self._network_guard.start()
        self.addCleanup(self._network_guard.stop)
        app = create_app(static_dir=self.data_dir / "empty-static")
        self.client = TestClient(app)
        self.db = DatabaseManager.get_instance()
        import src.services.sector_analysis_service as svc

        self._svc = svc
        # 场景冻结状态（基座继承的 self._freeze 装入；_arranged_sessions
        # 的时钟一致源——未冻结场景保持真实时刻行为不变）
        self._frozen_now: Optional[datetime] = None
        self.report_dir = self.data_dir / "reports" / "sector_analysis"

        def _fake_report_dir() -> Path:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            return self.report_dir

        self._report_dir_patcher = patch.object(svc, "get_report_dir",
                                                _fake_report_dir)
        self._report_dir_patcher.start()

    def tearDown(self) -> None:
        self._report_dir_patcher.stop()
        DatabaseManager.reset_instance()
        Config.reset_instance()
        for name, value in self._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp_dir.cleanup()

    def _freeze(self, now: datetime) -> None:
        """场景内唯一冻结入口（与 v99/v101 基座同模式）：冻结申万链时钟缝
        到指定时刻。一个测试场景只装一次——叠加冻结会让后装 patcher 经已
        替换的 `_session_context` 解析出前一冻结时刻的会话。"""
        self._frozen_now = now
        for patcher in _freeze_clocks(self._svc, now=now, ref_sessions=[]):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _arranged_sessions(self, count: int) -> List[str]:
        """夹具安排的时钟一致源：场景已冻结时按冻结时刻取严格日历，
        否则按真实时刻（与既有行为逐位一致）。"""
        if self._frozen_now is not None:
            sessions = recent_completed_trading_sessions(
                "cn", count, current_time=self._frozen_now)
        else:
            sessions = recent_completed_trading_sessions("cn", count)
        assert sessions is not None, "真实 XSHG 日历应可用（离线数据）"
        return [d.isoformat() for d in sessions]

    def _install_sw_stub(self, stub: _SwTransportStub) -> None:
        # 会话/趋势日期从场景时钟一致源取值（已冻结场景 = 冻结时刻的严格
        # 日历会话；未冻结场景 = 真实时刻，行为与既有逐位一致）
        stub.session_date = self._arranged_sessions(1)[0]
        stub._history_dates = self._arranged_sessions(20)
        self._sw_post_patcher = patch.object(sw, "_http_api_curl_post", stub)
        self._sw_token_patcher = patch.object(sw, "_resolve_access_token",
                                              stub.resolve_token)
        self._sw_post_patcher.start()
        self._sw_token_patcher.start()
        # 模块缝替换必须随场景 teardown 还原（叠加 patch 的套件级重装
        # 各自 addCleanup；本基座 patcher 最后停止，恢复真实模块函数）
        self.addCleanup(self._sw_post_patcher.stop)
        self.addCleanup(self._sw_token_patcher.stop)

    def _install_eastmoney_sentinels(self) -> None:
        """东财链全部入口缝替换为即时失败：成分边界路径必须零东财出站。"""
        import akshare as ak

        def _sentinel(name: str):
            def _raise(*_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError(f"申万路径出现东财出站：{name}")
            return _raise

        self._em_patchers = []
        for name in ("stock_individual_info_em", "stock_profile_cninfo",
                     "stock_board_industry_name_em", "stock_board_industry_cons_em",
                     "stock_board_industry_hist_em", "stock_zh_index_daily_em",
                     "stock_zh_index_daily_tx"):
            self._em_patchers.append(patch.object(ak, name, _sentinel(name)))
        for name in ("_fetch_board_claims", "_http_get_json", "_http_get_text",
                     "_fetch_trends2_snapshot", "_fetch_stock_quote",
                     "_fetch_board_quote", "_fetch_tencent_member_quotes"):
            self._em_patchers.append(
                patch.object(self._svc, name, _sentinel(name)))
        for patcher in self._em_patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


class TestGiantRawAmountNeverInf(SectorReportApiV100TestCase):
    """API/Markdown 反例：超大原始整数金额经公开边界判无效——API null、
    Markdown 无 inf（JSON/Markdown 一致性，b59bcb6d 复现反例）。"""

    def setUp(self) -> None:
        super().setUp()
        # 冻结到已完成会话盘后：wire 戳 22:00:46 与请求会话同一严格日历
        # 会话（盘中真实时段运行时请求会话不漂移到活跃当日）
        self._freeze(FROZEN_COMPLETED_NOW)

    def test_bj_independent_quote_giant_amount_api_and_markdown_consistent(
            self):
        stub = _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=BJ_TARGET_SEC, member_codes=COAL_MEMBERS)
        self._install_sw_stub(stub)  # 会话/趋势日期与 stub 路由就绪
        self._install_eastmoney_sentinels()
        orig_call = stub.__call__

        def _giant_call(url, headers, body_bytes, *, label):
            parsed = json.loads(body_bytes.decode())
            if ("real_time_quotation" in url
                    and parsed.get("codes") == BJ_TARGET_SEC):
                row = _quote_row(BJ_TARGET_SEC, stub.session_date, 1.5, 4.0e7)
                row["table"]["amount"] = [10 ** 400]
                return 200, json.dumps(
                    {"errorcode": 0, "errmsg": "", "tables": [row]}).encode()
            return orig_call(url, headers, body_bytes, label=label)

        self._sw_post_patcher = patch.object(sw, "_http_api_curl_post",
                                             _giant_call)
        self._sw_token_patcher = patch.object(
            sw, "_resolve_access_token", stub.resolve_token)
        self._sw_post_patcher.start()
        self._sw_token_patcher.start()
        self.addCleanup(self._sw_post_patcher.stop)
        self.addCleanup(self._sw_token_patcher.stop)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={BJ_TARGET}&stock_name=测试北交所标的"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        peers = analysis["sector_peers"]
        tiq = peers["target_independent_quote"]
        self.assertIsNotNone(tiq)
        self.assertIsNone(tiq["amount"])
        md = resp.json()["markdown"]
        self.assertNotIn("inf", md)
        self.assertIn("无效", md)  # 金额置为未知的缺口随载荷透出
        # 详情读回与下载一致（API null = Markdown 无 inf）
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertIsNone(
            detail.json()["data"]["analysis_json"]["sector_peers"]
            ["target_independent_quote"]["amount"])
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.text, md)


class TestEmSwitchZeroConstituentOutbound(SectorReportApiV100TestCase):
    """API 反例：东财切换成功时零被弃的 SW 成分/同业出站（amend 顺序）。"""

    def test_switched_report_has_no_constituent_outbound(self):
        from tests.test_sector_sw_compose_v100 import _em_core_stub
        from unittest.mock import patch as _patch

        stub = _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=SH_TARGET_SEC, member_codes=COAL_MEMBERS)
        stub.quotes_error = True  # 快照失败 → 目标日行情不可用
        self._install_sw_stub(stub)
        stub._history_dates = ["2026-11-10"]  # 趋势窗口不合格 → 趋势不可用
        self._install_eastmoney_sentinels()
        em_core = _em_core_stub()  # confirmed + 行情恢复 → 成功切换
        with _patch.object(self._svc, "_compose_eastmoney_core",
                           lambda code, *, budget: em_core):
            resp = self.client.post(
                "/api/v1/sector-analysis/generate"
                f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["research_outcome"], "switched_fallback")
        self.assertEqual(analysis["research_basis"], "eastmoney_board")
        # 切换路径：成分查询与成员行情批次零出站（仅核心链的 31 快照）
        self.assertEqual(len([1 for u, p in stub.calls
                              if "data_pool" in u]), 0)
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        self.assertEqual(len(rt_calls), 1)
        self.assertEqual(rt_calls[0]["codes"].split(","), SW31_CODES)


class TestBjUnknownBoundary(SectorReportApiV100TestCase):
    """北交所成分边界（unknown）：行业保留、同业行业参考、目标独立展示。"""

    def setUp(self) -> None:
        super().setUp()
        # 冻结到已完成会话盘后（安排/请求/断言同一严格日历会话）
        self._freeze(FROZEN_COMPLETED_NOW)

    def _stub(self) -> _SwTransportStub:
        return _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=BJ_TARGET_SEC, member_codes=COAL_MEMBERS)

    def test_full_loop_unknown_boundary_keeps_industry_and_flags_reference(self):
        stub = self._stub()
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={BJ_TARGET}&stock_name=测试北交所标的"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get("report_id"))
        analysis = body["analysis"]
        validated = SectorReportPayload.model_validate(analysis)
        self.assertEqual(validated.payload_version, 3)

        ident = analysis["identity"]
        self.assertEqual(ident["subject_status"], "confirmed")
        membership = ident["membership"]
        self.assertFalse(membership["verified"])
        self.assertIsNone(membership["verified_source"])
        self.assertEqual(membership["sw_membership"]["status"], "unknown")
        self.assertEqual(membership["sw_membership"]["target_secucode"],
                         BJ_TARGET_SEC)
        self.assertEqual(membership["sw_membership"]["observation_date"],
                         self._arranged_sessions(1)[0])

        # 行业行情与趋势保留
        intraday = analysis["background"]["intraday"]
        self.assertTrue(intraday["sector_found"])
        trend = analysis["sector_trend"]
        self.assertTrue(trend["eligible"])

        # 同业：行业参考 + 目标独立展示 + 名次留空 + 目标不进 items
        peers = analysis["sector_peers"]
        self.assertIsNotNone(peers)
        self.assertTrue(peers["eligible"])
        self.assertTrue(peers["industry_reference"])
        codes = [i["code"] for i in peers["items"]]
        self.assertNotIn(BJ_TARGET, codes)
        self.assertTrue(peers["items"])
        self.assertFalse(any(i["is_target"] for i in peers["items"]))
        self.assertIsNone(peers["target_in_amount_top5"])
        self.assertIsNone(peers["target_change_pct_rank"])
        tiq = peers["target_independent_quote"]
        self.assertIsNotNone(tiq)
        self.assertEqual(tiq["code"], BJ_TARGET)
        self.assertEqual(tiq["as_of_date"], self._arranged_sessions(1)[0])
        self.assertIsNotNone(tiq["change_pct"])
        self.assertIsNotNone(tiq["amount"])

        # 完整性与原因：成分边界如实标注部分
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        reasons = " ".join(analysis["assessment"]["reasons"])
        self.assertIn("成分状态未知", reasons)
        self.assertIn("行业参考", reasons)
        gaps = " ".join(analysis["gaps"])
        self.assertIn("成分", gaps)

        # 独立行情出站恰好一次（未确认成分时）；无东财出站（哨兵即时失败）
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        independent = [p for p in rt_calls if p["codes"] == BJ_TARGET_SEC]
        self.assertEqual(len(independent), 1)

        # 存档一致：详情读回与下载
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_analysis = detail.json()["data"]["analysis_json"]
        self.assertEqual(detail_analysis["sector_peers"]["industry_reference"],
                         True)
        self.assertEqual(detail_analysis["identity"]["membership"]
                         ["sw_membership"]["status"], "unknown")
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        md = download.text
        self.assertIn("成分状态未知", md)
        self.assertIn("行业参考", md)
        self.assertIn("目标股独立行情", md)
        self.assertIn(BJ_TARGET, md)
        self.assertIn(self._arranged_sessions(1)[0], md)

    def test_target_quote_failure_keeps_report_with_disclosed_gap(self):
        stub = self._stub()
        stub.target_independent = True
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        # 独立行情返回异日行（资格拒绝）→ 独立行情未取得缺口
        orig_call = stub.__call__

        def _call(url, headers, body_bytes, *, label):
            parsed = json.loads(body_bytes.decode())
            if ("real_time_quotation" in url
                    and parsed.get("codes") == BJ_TARGET_SEC):
                # 异日行情行（非目标会话日）：资格筛选拒绝，独立行情未取得
                rows = [_quote_row(BJ_TARGET_SEC, "2026-11-10", 1.5, 4.0e7)]
                return 200, json.dumps(_quotes_payload(rows)).encode()
            return orig_call(url, headers, body_bytes, label=label)

        self._sw_post_patcher = patch.object(sw, "_http_api_curl_post", _call)
        self._sw_token_patcher = patch.object(
            sw, "_resolve_access_token", stub.resolve_token)
        self._sw_post_patcher.start()
        self._sw_token_patcher.start()
        self.addCleanup(self._sw_post_patcher.stop)
        self.addCleanup(self._sw_token_patcher.stop)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={BJ_TARGET}&stock_name=测试北交所标的"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        self.assertIsNone(peers["target_independent_quote"])
        gaps = " ".join(peers["gaps"])
        self.assertIn("独立行情未取得", gaps)


class TestShIncludedLoop(SectorReportApiV100TestCase):
    """沪深 included：目标并入样本、名次可判、membership verified。"""

    def setUp(self) -> None:
        super().setUp()
        # 冻结到已完成会话盘后（安排/请求/断言同一严格日历会话）
        self._freeze(FROZEN_COMPLETED_NOW)

    def test_full_loop_included_merges_target_and_verifies_membership(self):
        stub = _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=SH_TARGET_SEC, member_codes=COAL_MEMBERS)
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        membership = analysis["identity"]["membership"]
        self.assertTrue(membership["verified"])
        self.assertEqual(membership["verified_source"], "sw_index_members")
        self.assertEqual(membership["sw_membership"]["status"], "included")
        peers = analysis["sector_peers"]
        self.assertFalse(peers["industry_reference"])
        self.assertIsNone(peers["target_independent_quote"])
        self.assertTrue(any(i["is_target"] for i in peers["items"]))
        # 受控成员行情：目标 change 1.0 与其余一致 → 并列；名次可判
        self.assertIsNotNone(peers["target_change_pct_rank"])
        # included 无目标独立行情出站
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        self.assertEqual(
            len([p for p in rt_calls if p["codes"] == SH_TARGET_SEC]), 0)
        # 成员行情批次出站（6 只成员 < 150 → 1 批）
        self.assertEqual(len(rt_calls), 2)
        # 存档一致
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(
            detail.json()["data"]["analysis_json"]["sector_peers"],
            analysis["sector_peers"])


class TestConstituentFailureLoop(SectorReportApiV100TestCase):
    """成分查询供应商失败：报告成功（部分）、membership 保守未知。"""

    def setUp(self) -> None:
        super().setUp()
        # 冻结到已完成会话盘后（安排/请求/断言同一严格日历会话）
        self._freeze(FROZEN_COMPLETED_NOW)

    def test_vendor_error_degrades_membership_not_report(self):
        stub = _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=SH_TARGET_SEC, member_codes=COAL_MEMBERS,
            members_error={"errorcode": -4001, "errmsg": "no data",
                           "tables": []})
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        membership = analysis["identity"]["membership"]
        self.assertFalse(membership["verified"])
        self.assertEqual(membership["sw_membership"]["status"], "unknown")
        peers = analysis["sector_peers"]
        self.assertTrue(peers["industry_reference"])
        self.assertEqual(peers["items"], [])
        self.assertTrue(peers["gaps"])
        # 成分失败 → 成员集空：无成员批次；目标未确认成分 → 独立行情仍尝试
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        self.assertEqual(len(rt_calls), 2)
        self.assertEqual(rt_calls[1]["codes"], SH_TARGET_SEC)
        self.assertIsNotNone(peers["target_independent_quote"])
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        # Markdown：成分未取得 ≠「响应未见目标股」（诊断如实区分）
        self.assertIn("同日成分未取得", resp.json()["markdown"])
        self.assertNotIn("未见目标股", resp.json()["markdown"])


class TestAmountOnlyMemberKeepsSample(SectorReportApiV100TestCase):
    """成分成员口径反例（#102 复审 Spec 2，wire → 报告全实链）：

    六只成分中成交额第一的成员涨跌幅缺失——修复前该行在资格层被整行剔除
    （第六名递补进前五、样本内名次照常输出）；修复后身份/会话/时间戳合格
    的 amount-only 成员保留有效金额参与选样，涨跌幅名次 withhold + 覆盖
    说明。"""

    def setUp(self) -> None:
        super().setUp()
        self._freeze(FROZEN_COMPLETED_NOW)

    def test_top_amount_member_missing_change_stays_in_sample(self):
        stub = _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=SH_TARGET_SEC, member_codes=COAL_MEMBERS,
            member_amounts={"601898.SH": 5.0e9},
            member_changes={"601898.SH": None})
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["identity"]["membership"]["verified"], True)
        peers = analysis["sector_peers"]
        codes = [i["code"] for i in peers["items"]]
        # 成交额第一的 amount-only 成员保留在样本（不递补第六名）
        self.assertIn("601898", codes)
        self.assertNotIn("601225", codes)
        top_member = next(i for i in peers["items"] if i["code"] == "601898")
        self.assertEqual(top_member["amount"], 5.0e9)
        self.assertIsNone(top_member["change_pct"])
        # 任一选中成员缺涨跌幅 → 名次 withhold + 覆盖说明（不伪完整排名）
        self.assertIsNone(peers["target_change_pct_rank"])
        gaps = " ".join(peers["gaps"])
        self.assertIn("有效比较范围", gaps)
        # 成员行情批次真实出站（31 快照 + 1 批），存档与下载一致
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        self.assertEqual(len(rt_calls), 2)
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["data"]["analysis_json"]["sector_peers"],
            peers)
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertIn("601898", download.text)

    def test_inconsistent_change_member_blanks_change_keeps_amount(self):
        """交叉复核不合格的成员涨跌幅（changeRatio=0.495、latest=100.5/
        preClose=100 恰差 0.005pp）同日行：成分口径只作废涨跌幅本身——
        有效成交额 5e9 保留参与选样（不剔除、不递补第六名），目标涨跌幅
        名次 withhold + 有效范围缺口（#102 复审 Spec）。"""
        stub = _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=SH_TARGET_SEC, member_codes=COAL_MEMBERS,
            member_amounts={"601898.SH": 5.0e9},
            member_changes={"601898.SH": 0.495})
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        orig_call = stub.__call__

        def _inconsistent_call(url, headers, body_bytes, *, label):
            parsed = json.loads(body_bytes.decode())
            if ("real_time_quotation" in url
                    and "601898.SH" in parsed.get("codes", "").split(",")):
                payload = json.loads(orig_call(
                    url, headers, body_bytes, label=label)[1].decode())
                for row in payload.get("tables") or []:
                    if row.get("thscode") == "601898.SH":
                        # 涨跌幅 0.495 对 latest 100.5/preClose 100 恰差 0.005pp
                        row["table"]["latest"] = [100.5]
                        row["table"]["preClose"] = [100.0]
                return 200, json.dumps(payload).encode()
            return orig_call(url, headers, body_bytes, label=label)

        self._sw_post_patcher = patch.object(sw, "_http_api_curl_post",
                                             _inconsistent_call)
        self._sw_token_patcher = patch.object(
            sw, "_resolve_access_token", stub.resolve_token)
        self._sw_post_patcher.start()
        self._sw_token_patcher.start()
        self.addCleanup(self._sw_post_patcher.stop)
        self.addCleanup(self._sw_token_patcher.stop)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        top = next(i for i in peers["items"] if i["code"] == "601898")
        self.assertEqual(top["amount"], 5.0e9)      # 有效金额保留参与选样
        self.assertIsNone(top["change_pct"])        # 只作废涨跌幅本身
        self.assertNotIn("601225", [i["code"] for i in peers["items"]])
        self.assertIsNone(peers["target_change_pct_rank"])  # 名次 withhold
        gaps = " ".join(peers["gaps"])
        self.assertIn("有效比较范围", gaps)
        # 存档一致
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(
            detail.json()["data"]["analysis_json"]["sector_peers"], peers)


class TestBjIndependentObservation(SectorReportApiV100TestCase):
    """目标独立行情发布门与空值观测（#102 复审 Spec）：行情行存在且时点/
    身份合格——有有效成交额（含已知零）或合格涨跌幅才发布数值块；数值
    全缺时不发布全空块，降为空值观测事实 + 同款明确缺口。"""

    def setUp(self) -> None:
        super().setUp()
        self._freeze(FROZEN_COMPLETED_NOW)

    def _generate_with_target_row(self, row: Optional[Dict[str, Any]]):
        stub = _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=BJ_TARGET_SEC, member_codes=COAL_MEMBERS)
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        orig_call = stub.__call__

        def _call(url, headers, body_bytes, *, label):
            parsed = json.loads(body_bytes.decode())
            if ("real_time_quotation" in url
                    and parsed.get("codes") == BJ_TARGET_SEC):
                if row is None:
                    return 200, json.dumps(_quotes_payload([])).encode()
                return 200, json.dumps(_quotes_payload([row])).encode()
            return orig_call(url, headers, body_bytes, label=label)

        self._sw_post_patcher = patch.object(sw, "_http_api_curl_post", _call)
        self._sw_token_patcher = patch.object(
            sw, "_resolve_access_token", stub.resolve_token)
        self._sw_post_patcher.start()
        self._sw_token_patcher.start()
        self.addCleanup(self._sw_post_patcher.stop)
        self.addCleanup(self._sw_token_patcher.stop)
        return self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={BJ_TARGET}&stock_name=测试北交所标的"
        )

    def test_all_null_row_no_published_quote_no_observation(self):
        """有效 BJ 时点 + 空值表：不发布全空数值块，也不发布任何空值观测
        字段——「独立行情未取得」明确缺口承载；登记表不注册任何目标事实
        （compose_v101 登记表级断言覆盖）。"""
        row = _quote_row(BJ_TARGET_SEC, self._arranged_sessions(1)[0], None,
                         amount=0.0)
        # 全空数值：成交额与涨跌幅均缺失（时点/身份合格）
        row["table"]["changeRatio"] = [None]
        row["table"]["amount"] = [None]
        resp = self._generate_with_target_row(row)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        peers = analysis["sector_peers"]
        self.assertIsNone(peers["target_independent_quote"])
        self.assertNotIn("target_independent_observation", peers)
        gaps = " ".join(peers["gaps"])
        self.assertIn("独立行情未取得", gaps)
        self.assertIn("均缺失/无效", gaps)
        # 存档一致（严格载荷校验随 200 通过）
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(
            detail.json()["data"]["analysis_json"]["sector_peers"], peers)

    def test_zero_amount_with_valid_change_publishes_quote(self):
        """已知零额 + 合格涨跌幅：发布数值块（零额合法保留 0，含 0 语义）。"""
        row = _quote_row(BJ_TARGET_SEC, self._arranged_sessions(1)[0], 1.5,
                         amount=0.0)
        resp = self._generate_with_target_row(row)
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        tiq = peers["target_independent_quote"]
        self.assertIsNotNone(tiq)
        self.assertEqual(tiq["amount"], 0.0)
        self.assertEqual(tiq["change_pct"], 1.5)
        self.assertNotIn("target_independent_observation", peers)


class TestLongAncillaryTradeTimeArchives(SectorReportApiV100TestCase):
    """快照来源文字归一（#102 复审 Standards 3，API 存档边界）：

    辅助 tradeTime 单元格携带 250 字符任意文本时，快照来源自已解析合格的
    完整供应商时间戳生成——snapshot_source 不超 Pydantic 上限（200），
    报告实际存档可读回（修复前载荷校验失败 → 500 且不存档）。"""

    def setUp(self) -> None:
        super().setUp()
        self._freeze(FROZEN_COMPLETED_NOW)

    def test_long_ancillary_trade_time_report_archives(self):
        stub = _SwTransportStub(
            industry_by_level={"1": "煤炭", "2": "煤炭开采", "3": "动力煤"},
            target_secucode=SH_TARGET_SEC, member_codes=COAL_MEMBERS)
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        orig_call = stub.__call__

        def _long_trade_time_call(url, headers, body_bytes, *, label):
            parsed = json.loads(body_bytes.decode())
            if "real_time_quotation" in url \
                    and parsed.get("codes") == ",".join(SW31_CODES):
                payload = json.loads(orig_call(
                    url, headers, body_bytes, label=label)[1].decode())
                for row in payload.get("tables") or []:
                    row.setdefault("table", {})["tradeTime"] = ["x" * 250]
                return 200, json.dumps(payload).encode()
            return orig_call(url, headers, body_bytes, label=label)

        self._sw_post_patcher = patch.object(sw, "_http_api_curl_post",
                                             _long_trade_time_call)
        self._sw_token_patcher = patch.object(
            sw, "_resolve_access_token", stub.resolve_token)
        self._sw_post_patcher.start()
        self._sw_token_patcher.start()
        self.addCleanup(self._sw_post_patcher.stop)
        self.addCleanup(self._sw_token_patcher.stop)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate"
            f"?stock_code={SH_TARGET}&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        intraday = resp.json()["analysis"]["background"]["intraday"]
        source = intraday.get("snapshot_source") or ""
        self.assertLessEqual(len(source), 200)
        self.assertNotIn("xxxx", source)
        self.assertIn("22:00:46", source)  # 已验证时间戳生成来源文字
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(resp.json().get("report_id"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
