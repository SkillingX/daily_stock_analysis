# -*- coding: utf-8 -*-
"""板块申万口径公开 API 闭环（issue #99 主测试缝）。

从公开板块 API 发起生成，经真实申万编排/资格/缺失逻辑、严格响应校验、
临时 DB/文件、详情读回与下载观察。测试内容：

- 正常申万链路（归属+31 指数同日快照+严格日历趋势）：生成→保存→详情
  读回→下载一致；载荷 v3/research_basis/真实日期/阶段范围标注；
  **零东财行业与行情出站**（东财链全部入口缝替换为即时失败哨兵）；
- 盘中参考口径：目标交易日无合格行情、最近已完成交易日有合格行 →
  snapshot_date=参考日 + 缺口 + 报告为部分；
- 部分覆盖（仅目标行业合格）：不显示全量名次 + 覆盖缺口（无东财出站）；
- 存量 v2 载荷（无 research_basis）严格校验读回兼容；
- 共享边界回归：申万链路生成不触碰东财链路（含个股日线出站缝）。

iFinD 标准 HTTP 传输与认证在模块缝替换（响应形状取自 2026-11-18 真实
脱敏样本）；严格交易日历用真实 exchange-calendars；目录匹配/日期合格性/
交叉复核/严格校验/渲染/持久化不 mock。
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


# 冻结时刻：已完成会话盘后（任意墙钟时间下会话/资格判定确定性）。
# v99/v100 受控 wire 戳统一为该会话 22:00:46；供应商现行合同按**活跃会话**
# 归属报价——盘中真实时段运行时，未冻结场景的请求会话漂移到当日，与夹具
# 安排/断言依赖的已完成会话不一致。相关场景经 self._freeze 显式冻结到同一
# 严格日历会话（安排、请求与断言一致源）。
FROZEN_COMPLETED_NOW = datetime(2026, 11, 18, 22, 30, 0)


def _frozen_session_dates(count: int) -> List[Any]:
    """冻结时刻下最近 count 个已完成交易日（真实严格日历，最新在前）。"""
    sessions = recent_completed_trading_sessions(
        "cn", count, current_time=FROZEN_COMPLETED_NOW)
    assert sessions is not None, "真实 XSHG 日历应可用（离线数据）"
    return [d.isoformat() for d in sessions]


def _history_payload(codes: List[str], dates: List[str]) -> Dict[str, Any]:
    """cmd_history_quotation 响应：每个代码 20 日递增收盘（真实形状）。"""
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


def _quote_row(thscode: str, date: str, change: float) -> Dict[str, Any]:
    latest = round(100.0 * (1 + change / 100.0), 3)
    row = {
        "marketCategory": "1", "pricetype": 1, "thscode": thscode,
        "time": [f"{date} 22:00:46"],
        "table": {
            "changeRatio": [change], "amount": [1.0e9 + len(thscode)],
            "tradeTime": ["22:00:46"], "latest": [latest], "preClose": [100.0],
            "turnoverRatio": [2.0], "riseCount": [20.0], "fallCount": [10.0],
        },
    }
    return row


def _basic_payload(name: str) -> Dict[str, Any]:
    return {
        "errorcode": 0, "errmsg": "",
        "tables": [{"thscode": "600188.SH", "time": [],
                    "table": {"ths_the_sw_industry_stock": [name]}}],
    }


class _SwTransportStub:
    """申万模块缝替身：按端点/层级返回预置响应，记录全部请求。

    issue #100 扩展：data_pool（p03473 成分）与按请求 codes 的成员行情
    路由——正常申万链路（成分+成员行情）与 #99 行为共用同一替身。
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.basic_by_level: Dict[str, str] = {}
        self.quotes: Optional[List[Dict[str, Any]]] = None
        self.history_codes: List[str] = []
        self.history_dates: List[str] = []
        self.session_date: str = ""
        self.member_codes: List[str] = ["000552.SZ", "000723.SZ",
                                        "600188.SH", "601225.SH",
                                        "601898.SH", "002128.SZ"]

    def _members_payload(self) -> Dict[str, Any]:
        return {"errorcode": 0, "errmsg": "", "tables": [{
            "table": {
                "p03473_f001": [self.session_date] * len(self.member_codes),
                "p03473_f002": list(self.member_codes),
                "p03473_f003": [f"成员{c[:6]}" for c in self.member_codes],
            },
        }]}

    def __call__(self, url: str, headers: Dict[str, str],
                 body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
        assert body is not None
        parsed = json.loads(body.decode())
        self.calls.append((url, parsed))
        if "basic_data_service" in url:
            level = parsed["indipara"][0]["indiparams"][0]
            name = self.basic_by_level[level]
            return 200, json.dumps(_basic_payload(name)).encode()
        if "data_pool" in url:
            return 200, json.dumps(self._members_payload()).encode()
        if "real_time_quotation" in url:
            codes = parsed["codes"].split(",")
            if codes == [c for c, _ in sw.SW_L1_DIRECTORY]:
                return 200, json.dumps(_quotes_payload(self.quotes or [])).encode()
            # 成员批次 / 目标独立行情：按请求 codes 生成同日行情行
            rows = [_quote_row(c, self.session_date, 1.0) for c in codes]
            return 200, json.dumps(_quotes_payload(rows)).encode()
        if "cmd_history_quotation" in url:
            self.history_codes = parsed["codes"].split(",")
            return 200, json.dumps(_history_payload(
                self.history_codes, self.history_dates)).encode()
        raise AssertionError(f"未预置的申万端点: {url}")

    @staticmethod
    def resolve_token(refresh_token: str, *, force_refresh: bool = False) -> str:
        return "access-token-stub"


class SectorReportApiV99TestCase(unittest.TestCase):
    """临时 DB/报告目录 + 申万模块缝替身 + 东财哨兵的公开 API 测试基座。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "sector_api_v99_test.db"
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
            # 板块 iFinD 标准 HTTP 入口（凭据 + 单次超时；与本票板块入口
            # 配置一致，不启用 ENABLE_IFIND_HTTP_API 日线开关）
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
        # 意外出网守卫：任何 TCP 连接即失败（申万传输在模块缝替换）。
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
        # 场景冻结状态（self._freeze 装入；_arranged_sessions 的时钟一致源）
        self._frozen_now: Optional[datetime] = None
        self._orig_report_dir = svc._REPORT_DIR
        self.report_dir = self.data_dir / "reports" / "sector_analysis"

        def _fake_report_dir() -> Path:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            return self.report_dir

        svc._REPORT_DIR = self.report_dir
        self._report_dir_patcher = patch.object(svc, "get_report_dir",
                                                _fake_report_dir)
        self._report_dir_patcher.start()

    def _install_clock(self, svc_module, now: datetime):
        # 会话序列必须与冻结时钟一致（真实严格日历 + 冻结时刻取值）：
        # sessions = 冻结时刻下的已完成交易日（不含盘中未收盘当日）；
        # frozen_ctx.session_date = 冻结时刻的报价归属会话日（盘中=当日）
        real_sessions = recent_completed_trading_sessions("cn", 25, current_time=now)
        assert real_sessions is not None, "真实 XSHG 日历应可用（离线数据）"
        sessions = [d.isoformat() for d in real_sessions]
        frozen_ctx = svc_module._session_context(now)
        assert frozen_ctx is not None
        for patcher in _freeze_clocks(svc_module, now=now, ref_sessions=[]):
            patcher.start()
            self.addCleanup(patcher.stop)
        return sessions, frozen_ctx

    def _freeze(self, now: datetime) -> None:
        """场景内唯一冻结入口（与 v101 基座同模式）：冻结申万链时钟缝到
        指定时刻。一个测试场景只装一次——叠加冻结会让后装 patcher 经已
        替换的 `_session_context` 解析出前一冻结时刻的会话（显式盘中测试
        用各自的 `_install_clock`，不与类级冻结叠加）。"""
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

    def tearDown(self) -> None:
        self._report_dir_patcher.stop()
        self._svc._REPORT_DIR = self._orig_report_dir
        DatabaseManager.reset_instance()
        Config.reset_instance()
        for name, value in self._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp_dir.cleanup()

    # ---- 申万模块缝 ----

    def _install_sw_stub(self, stub: _SwTransportStub) -> None:
        self._sw_post_patcher = patch.object(sw, "_http_api_curl_post", stub)
        self._sw_token_patcher = patch.object(sw, "_resolve_access_token",
                                              stub.resolve_token)
        self._sw_post_patcher.start()
        self._sw_token_patcher.start()
        # 模块缝替换随场景 teardown 还原（套件级重装各自 addCleanup，
        # 本基座 patcher 最后停止恢复真实模块函数）
        self.addCleanup(self._sw_post_patcher.stop)
        self.addCleanup(self._sw_token_patcher.stop)

    # ---- 东财零出站哨兵 ----

    def _install_eastmoney_sentinels(self) -> None:
        """东财链全部入口缝替换为即时失败：申万正常路径必须零东财出站。"""
        import akshare as ak

        def _sentinel(name: str):
            def _raise(*_args: Any, **_kwargs: Any) -> Any:
                import traceback as _tb

                _tb.print_stack()
                raise AssertionError(f"申万正常路径出现东财出站：{name}")
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


class TestSwRouteClosedLoop(SectorReportApiV99TestCase):
    """正常申万链路：生成→保存→读回→下载一致，零东财出站。"""

    def _prepare_stub(self) -> _SwTransportStub:
        # 安排/请求/断言同一严格日历会话（冻结时刻的已完成交易日）：
        # wire 戳 22:00:46 与冻结接收钟 22:30 满足已完结会话新鲜度纪律
        sessions = _frozen_session_dates(20)
        session_date = sessions[0]
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        stub.session_date = session_date
        rows = []
        changes = [round(-14.0 + i, 2) for i in range(31)]
        target_change = -0.867
        for (code, _name), ch in zip(sw.SW_L1_DIRECTORY, changes):
            if code == "801950.SL":
                ch = target_change
            rows.append(_quote_row(code, session_date, ch))
        stub.quotes = rows
        stub.history_dates = sessions
        return stub

    def test_generate_save_readback_download_full_loop(self):
        # 冻结到已完成会话盘后（供应商现行合同按活跃会话归属报价：盘中
        # 真实时段运行时请求会话不再漂移到当日，安排/请求/断言同一会话）
        self._freeze(FROZEN_COMPLETED_NOW)
        stub = self._prepare_stub()
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        analysis = body["analysis"]
        # 严格载荷契约校验（响应模型已校验，此处显式复核版本/口径字段）
        validated = SectorReportPayload.model_validate(analysis)
        self.assertEqual(validated.payload_version, 3)
        self.assertEqual(validated.research_basis, "sw2021_l1")
        ident = analysis["identity"]
        self.assertEqual(ident["subject_status"], "confirmed")
        self.assertEqual(ident["classification_system"], "申万 2021 一级行业")
        self.assertEqual(ident["classification_level"], "L1")
        self.assertEqual(ident["classification_version"],
                         sw.SW_L1_DIRECTORY_VERSION)
        self.assertEqual(ident["board_code"], "801950.SL")
        self.assertEqual(ident["board_name"], "煤炭")
        self.assertEqual(ident["vendor_as_of"], _frozen_session_dates(1)[0])
        # 31/31 同日合格 → 全量名次 + 真实日期
        intraday = analysis["background"]["intraday"]
        self.assertTrue(intraday["sector_found"])
        self.assertEqual(intraday["snapshot_date"], _frozen_session_dates(1)[0])
        self.assertEqual(intraday["total_boards"], 31)
        self.assertIsNotNone(intraday["rank_of"])
        self.assertEqual(intraday["row"]["board_code"], "801950.SL")
        self.assertIn("iFinD", intraday["snapshot_source"] or "")
        # 趋势：严格日历 20 交易日，行业+基准各有区间收益+相对
        trend = analysis["sector_trend"]
        self.assertTrue(trend["eligible"])
        self.assertEqual(trend["qualified_sessions"], 20)
        self.assertIsNotNone(trend["period_return_pct"])
        self.assertIsNotNone(trend["benchmark"]["period_return_pct"])
        self.assertIsNotNone(trend["relative_return_pp"])
        self.assertEqual(trend["source"], "iFinD 标准HTTP指数日线")
        # 成分边界（issue #100）：included + 同业块接入；研究块随载荷透出
        # （issue #101：搜索入口未配置 → 催化有界降级块，综合 no_qualified）
        membership = ident["membership"]
        self.assertTrue(membership["verified"])
        self.assertEqual(membership["verified_source"], "sw_index_members")
        self.assertEqual(membership["sw_membership"]["status"], "included")
        self.assertEqual(membership["sw_membership"]["index_code"], "801950.SL")
        peers = analysis["sector_peers"]
        self.assertIsNotNone(peers)
        self.assertTrue(peers["eligible"])
        self.assertFalse(peers["industry_reference"])
        self.assertTrue(any(i["is_target"] for i in peers["items"]))
        self.assertIsNotNone(peers["target_change_pct_rank"])
        cat = analysis["sector_catalysts"]
        self.assertIsNotNone(cat)
        self.assertTrue(cat["eligible"])
        self.assertEqual(cat["items"], [])
        self.assertTrue(any("搜索入口未配置" in g for g in cat["gaps"]))
        syn = analysis["sector_synthesis"]
        self.assertIsNotNone(syn)
        self.assertEqual(syn["status"], "no_qualified_facts")
        reasons = " ".join(analysis["assessment"]["reasons"])
        self.assertIn("行业催化未取得", reasons)
        self.assertNotIn("同业比较与指数成分验证", reasons)
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        # 归属请求按层级单独查询（3 次）；#104 计数核验经同一端点单独出站
        basic_calls = [p for u, p in stub.calls if "basic_data_service" in u]
        self.assertEqual(len(basic_calls), 4)
        count_calls = [p for p in basic_calls
                       if p["indipara"][0]["indicator"]
                       == "ths_constituent_num_index"]
        self.assertEqual(len(count_calls), 1)  # 单指数绑定，恰好一次
        rt_calls = [p for u, p in stub.calls if "real_time_quotation" in u]
        # 31 指数快照 + L1 成员批次（included 链均无独立行情）；L2 成员批次
        # 为 0——issue #105 修复：二级名单与一级同名单时成员行情全复用
        # （带交易所完整代码、同报告目标交易日），零二级行情批次出站
        self.assertEqual(len(rt_calls), 2)
        self.assertEqual(len([1 for u, p in stub.calls
                              if "data_pool" in u]), 2)  # L1 + L2 成分各一次
        # issue #103：原一级 20 点趋势出站 + 必需研究后的多窗口（5/20/60 日）
        # 出站各一次；多窗口为新增可选出站，不替代原 20 点请求
        self.assertEqual(len([1 for u, p in stub.calls
                              if "cmd_history_quotation" in u]), 2)

        # 存档：详情读回与下载与生成一致（同一载荷/同一 Markdown）
        rows, total = self.db.list_sector_analysis_reports()
        self.assertEqual(total, 1)
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_analysis = detail.json()["data"]["analysis_json"]
        self.assertEqual(detail_analysis["research_basis"], "sw2021_l1")
        self.assertEqual(detail_analysis["identity"]["board_code"], "801950.SL")
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        md = download.text
        self.assertIn("申万 2021 一级行业", md)
        self.assertIn("煤炭（801950.SL）", md)
        self.assertIn(_frozen_session_dates(1)[0], md)
        self.assertNotIn("待后续版本接入", md)
        # 正文行完整性（trim_blocks 粘连回归）：列表项各占一行
        self.assertIn("三级参考：动力煤\n- 成分验证：", md)
        self.assertIn("\n- 行情日期：**", md)
        self.assertNotIn("行情日期：**2026", md.replace(
            f"行情日期：**{_frozen_session_dates(1)[0]}**", ""))
        # 催化章节渲染（issue #101）：搜索入口未配置的降级缺口如实呈现
        self.assertIn("## 四、行业催化", md)
        self.assertIn("搜索入口未配置", md)
        self.assertIn("申万口径研究说明", md)
        self.assertEqual(download.headers.get("content-type"),
                         "text/markdown; charset=utf-8")

    def test_reference_date_intraday_marks_partial_with_gap(self):
        """盘中目标日无合格行情、立即上一已完成交易日有目标行：参考口径。

        amend 四：参考口径仅适用于盘中目标会话，且参考 cohort 必须含目标
        指数行；趋势窗口保持最近 20 个已完成交易日合格（任一核心行情可用
        即不兜底）；本用例同时验证参考场景零东财出站。
        """
        from datetime import datetime

        now = datetime(2026, 11, 19, 10, 30, 0)
        sessions, frozen_ctx = self._install_clock(self._svc, now=now)
        session_date = frozen_ctx.session_date.isoformat()  # 盘中当日
        ref_date = sessions[0]  # 立即上一已完成交易日
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        stub.quotes = [_quote_row(code, ref_date, 1.2)
                       for code, _n in sw.SW_L1_DIRECTORY]
        stub.history_dates = sessions[:20]
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        intraday = analysis["background"]["intraday"]
        self.assertTrue(intraday["sector_found"])
        self.assertEqual(intraday["snapshot_date"], ref_date)
        self.assertEqual(intraday["snapshot_mode"], "reference")
        self.assertEqual(intraday["row"]["change_pct"], 1.2)
        joined = " ".join(intraday["gaps"]) + " ".join(
            analysis["assessment"]["reasons"])
        self.assertIn(ref_date, joined)
        self.assertIn(session_date, joined)
        self.assertEqual(analysis["assessment"]["completeness"], "partial")

    def test_post_close_target_day_gets_no_reference(self):
        """盘后目标（会话已完结）目标行缺失：不给上一交易日参考（amend 四）。

        参考口径仅适用于盘中目标会话；盘后/休市目标缺行情如实缺口，
        snapshot_mode 不标注 reference（模式从实际选用数据推导）。
        """
        from datetime import datetime

        now = datetime(2026, 11, 19, 22, 30, 0)  # 盘后（供应商 22:00 推送已接收）
        sessions, frozen_ctx = self._install_clock(self._svc, now=now)
        session_date = frozen_ctx.session_date.isoformat()  # 已完结当日
        prev_date = sessions[1]  # 上一交易日（sessions[0] 此时已是当日）
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        rows = []
        for (code, _n) in sw.SW_L1_DIRECTORY:
            if code == "801950.SL":
                # 目标指数只有上一交易日盘后行（目标日行缺失）
                row = _quote_row(code, prev_date, 1.2)
                row["time"] = [f"{prev_date} 22:00:00"]
                row["table"]["tradeTime"] = ["22:00:00"]
                rows.append(row)
            else:
                rows.append(_quote_row(code, session_date, 0.5))
        stub.quotes = rows
        stub.history_dates = sessions[:20]
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        intraday = analysis["background"]["intraday"]
        # 无目标合格行（当日缺失、盘后不前向参考）：不发布行情块——
        # sector_found=false、快照元数据（日期/口径/来源）全空，不以
        # N/A 数值表冒充实际行情；其余指数覆盖事实与盘后不参考原因保留
        # 在缺口（#102 复审三轮修正契约）
        self.assertFalse(intraday["sector_found"])
        self.assertIsNone(intraday["snapshot_mode"])
        self.assertIsNone(intraday["snapshot_date"])
        self.assertIsNone(intraday["snapshot_source"])
        self.assertTrue(any("已完成会话" in g for g in intraday["gaps"]))
        gaps_text = " ".join(intraday["gaps"])
        self.assertIn("目标指数 801950.SL", gaps_text)
        self.assertIn("其余 30/31 个指数取得当日合格行情", gaps_text)
        self.assertEqual(analysis["research_basis"], "sw2021_l1")

    def test_reference_without_target_row_not_applied(self):
        """盘中但立即上一已完成交易日无目标指数行：参考口径不适用。"""
        from datetime import datetime

        now = datetime(2026, 11, 19, 10, 30, 0)
        sessions, _frozen_ctx = self._install_clock(self._svc, now=now)
        ref_date = sessions[0]
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        rows = [[_quote_row(code, ref_date, 0.5)]
                for code, _n in sw.SW_L1_DIRECTORY if code != "801950.SL"]
        stub.quotes = [r for pair in rows for r in pair]
        stub.history_dates = sessions[:20]
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        intraday = analysis["background"]["intraday"]
        self.assertFalse(intraday["sector_found"])  # 无主体参考不给
        self.assertIsNone(intraday["snapshot_mode"])
        self.assertTrue(any("无目标指数合格行情" in g
                            for g in intraday["gaps"]))

    def test_partial_coverage_keeps_facts_without_full_rank_no_em(self):
        """仅目标行业合格（1/31，目标口径）：保留事实+覆盖缺口，不给全量
        名次，零东财出站。冻结到已完成会话盘后——盘中真实时段运行时请求
        会话不漂移到活跃当日（否则本用例会静默漂成参考口径场景）。"""
        self._freeze(FROZEN_COMPLETED_NOW)
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        sessions = _frozen_session_dates(20)
        session_date = sessions[0]
        stub.quotes = [_quote_row("801950.SL", session_date, -0.867)]
        stub.history_dates = sessions
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        intraday = analysis["background"]["intraday"]
        self.assertTrue(intraday["sector_found"])
        # 冻结场景下的目标口径钉住（不漂移为参考口径）
        self.assertEqual(intraday["snapshot_mode"], "target")
        self.assertEqual(intraday["snapshot_date"],
                         _frozen_session_dates(1)[0])
        self.assertIsNone(intraday["rank_of"])
        self.assertTrue(any("全量名次" in g or "合格行情" in g
                            for g in intraday["gaps"]))
        self.assertEqual(analysis["research_basis"], "sw2021_l1")


class TestLegacyPayloadCompat(SectorReportApiV99TestCase):
    """存量 v2 载荷（无 research_basis/snapshot_date）严格校验读回兼容。"""

    def test_v2_payload_without_new_fields_validates(self):
        legacy = {
            "payload_version": 2,
            "stock_code": "600519", "stock_name": "贵州茅台",
            "generated_at": "2026-11-01T10:00:00",
            "identity": {"subject_status": "confirmed"},
            "background": {"policy": {"lean": None, "label": None, "gaps": []},
                           "base_rate": {"is_default": True,
                                         "table_version": "v1.0",
                                         "table_updated": "2026-09-29",
                                         "table_industry_count": 21},
                           "intraday": {"sector_found": True}},
            "concepts": {"items": []},
            "assessment": {"status": "unscored",
                           "completeness": "complete", "reasons": []},
            "identification": {},
            "policy": {},
            "base_rate": {"is_default": True, "table_version": "v1.0",
                          "table_updated": "2026-09-29",
                          "table_industry_count": 21},
            "prosperity": {"sector_found": True},
            "sector_score": None, "band": None, "beta_meaning": None,
            "weights": None, "prosperity_score": None, "gaps": [],
        }
        validated = SectorReportPayload.model_validate(legacy)
        self.assertIsNone(validated.research_basis)
        self.assertEqual(validated.payload_version, 2)

    def test_v3_rejects_unknown_research_basis(self):
        with self.assertRaises(Exception):
            SectorReportPayload.model_validate({
                "payload_version": 3, "research_basis": "unknown_source",
                "stock_code": "600519", "stock_name": "贵州茅台",
                "generated_at": "2026-11-19T10:00:00",
                "identity": {"subject_status": "confirmed"},
                "background": {"policy": {}, "base_rate": {}, "intraday": {}},
                "concepts": {}, "assessment": {"reasons": []},
                "identification": {}, "policy": {}, "base_rate": {},
                "prosperity": {}, "gaps": [],
            })


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# amend 回归：会话时间/新鲜度纪律、参考口径显式化、畸形 wire 降级
# （公开 API 缝；时钟经既有注入缝冻结：_session_context/_received_at_now/
#   recent_completed_trading_sessions）
# ---------------------------------------------------------------------------


def _freeze_clocks(svc_module, *, now: datetime, ref_sessions: List[Any]):
    """冻结申万链的三个时钟缝（会话上下文/接收钟/严格日历窗口）。

    会话上下文取真实日历在**冻结时刻**的解析结果（盘中=当日会话、盘后/
    非交易日=最近已完成会话）；严格日历窗口同样按冻结时刻取值。
    """
    from src.core.trading_calendar import get_market_now

    real_ctx = svc_module._session_context(now)
    assert real_ctx is not None, "真实 XSHG 日历应可用（离线数据）"

    def fake_session_context(now_dt=None):
        return real_ctx

    real_sessions_fn = recent_completed_trading_sessions

    def fake_sessions(market, count, *, current_time=None):
        sessions = real_sessions_fn(market, count + 1, current_time=now)
        assert sessions is not None
        return sessions[:count]

    patches = [
        patch.object(svc_module, "_session_context", fake_session_context),
        patch.object(svc_module, "_received_at_now",
                     lambda: get_market_now("cn", current_time=now).replace(
                         tzinfo=None)),
        patch.object(svc_module, "recent_completed_trading_sessions",
                     fake_sessions),
    ]
    return patches


class TestAmendTimingAndReference(SectorReportApiV99TestCase):
    """amend 回归（issue #99 二轮）：时间/新鲜度 + 参考口径显式化。"""

    def test_intraday_current_quote_is_target_mode_not_reference(self):
        """盘中即时行情（开盘≤时间戳≤接收）：target 口径，正文不标参考口径。"""
        now = datetime(2026, 11, 19, 10, 30, 0)
        sessions, frozen_ctx = self._install_clock(self._svc, now=now)
        session_date = frozen_ctx.session_date.isoformat()  # 盘中当日
        prev_date = sessions[0]  # 最近已完成交易日（趋势窗口截止）
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        rows = []
        for (code, _n) in sw.SW_L1_DIRECTORY:
            ch = -0.867 if code == "801950.SL" else 1.0
            row = _quote_row(code, session_date, ch)
            row["time"] = [f"{session_date} 10:29:00"]
            row["table"]["tradeTime"] = ["10:29:00"]
            rows.append(row)
        stub.quotes = rows
        stub.history_dates = sessions[:20]  # 严格日历 20 个已完成交易日（截止上一日）
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        intraday = analysis["background"]["intraday"]
        self.assertTrue(intraday["sector_found"])
        self.assertEqual(intraday["snapshot_date"], session_date)
        self.assertEqual(intraday["snapshot_mode"], "target")
        trend = analysis["sector_trend"]
        self.assertEqual(trend["end_date"], prev_date)
        md = resp.json()["markdown"]
        self.assertIn(f"行情日期：**{session_date}**", md)
        self.assertNotIn("参考口径", md)  # 目标日盘中合格行情不是参考口径
        self.assertIn("iFinD", md)

    def test_stale_completed_session_morning_quote_rejected(self):
        """已完结会话的上午盘中旧快照（未过收盘）拒绝：不进入参考 cohort。"""
        from datetime import datetime

        now = datetime(2026, 11, 19, 10, 30, 0)
        sessions, frozen_ctx = self._install_clock(self._svc, now=now)
        prev_date = sessions[0]  # 最近已完成交易日（参考会话候选）
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        rows = []
        for (code, _n) in sw.SW_L1_DIRECTORY:
            row = _quote_row(code, prev_date, 1.2)
            row["time"] = [f"{prev_date} 10:00:00"]  # 上一日盘中旧快照（未过收盘）
            row["table"]["tradeTime"] = ["10:00:00"]
            rows.append(row)
        stub.quotes = rows
        stub.history_dates = sessions[:20]
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        intraday = analysis["background"]["intraday"]
        # 旧快照全部被拒（目标日/参考日均无合格行）→ 当日行情未产出 + 部分
        self.assertFalse(intraday["sector_found"])
        self.assertIsNone(intraday["snapshot_date"])
        self.assertEqual(analysis["research_basis"], "sw2021_l1")
        self.assertTrue(any("快照" in g or "行情" in g for g in intraday["gaps"]))

    def test_reference_activates_on_missing_target_row_from_immediate_prev_session(self):
        """目标指数目标日行缺失：立即上一已完成交易日的同日 cohort 参考激活。"""
        from datetime import datetime

        now = datetime(2026, 11, 19, 10, 30, 0)
        sessions, frozen_ctx = self._install_clock(self._svc, now=now)
        session_date = frozen_ctx.session_date.isoformat()  # 盘中当日
        prev_date = sessions[0]  # 立即上一已完成交易日
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        rows = []
        for (code, _n) in sw.SW_L1_DIRECTORY:
            if code == "801950.SL":
                # 目标指数只有上一交易日盘后合格行（目标日行缺失）
                row = _quote_row(code, prev_date, 1.2)
                row["time"] = [f"{prev_date} 22:00:00"]
                row["table"]["tradeTime"] = ["22:00:00"]
            else:
                row = _quote_row(code, session_date, 0.5)
                row["time"] = [f"{session_date} 10:20:00"]
                row["table"]["tradeTime"] = ["10:20:00"]
            rows.append(row)
        stub.quotes = rows
        stub.history_dates = sessions[:20]
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        intraday = analysis["background"]["intraday"]
        # 目标行缺失 → 参考口径激活（其他指数有当日行也不拼接两日截面）
        self.assertTrue(intraday["sector_found"])
        self.assertEqual(intraday["snapshot_mode"], "reference")
        self.assertEqual(intraday["snapshot_date"], prev_date)
        self.assertEqual(intraday["row"]["change_pct"], 1.2)
        md = resp.json()["markdown"]
        self.assertIn("参考口径", md)
        self.assertIn(prev_date, md)
        # 参考口径（quote_target_ok=False）+ 趋势合格 → 不触发东财
        self.assertEqual(analysis["research_basis"], "sw2021_l1")

    def test_malformed_quotes_wire_degrades_not_500(self):
        """快照 wire 畸形（tables 元素非对象）：降级缺口 + 部分报告，不 500。"""
        from datetime import datetime

        now = datetime(2026, 11, 19, 1, 35, 0)
        sessions, _frozen_ctx = self._install_clock(self._svc, now=now)
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        stub.quotes = []  # 触发后 stub 直接返回畸形 tables
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()

        # 直接改写 quotes 端点响应为畸形形状
        def malformed_quotes(url, headers, body, *, label):
            parsed = json.loads(body.decode())
            stub.calls.append((url, parsed))
            if "real_time_quotation" in url:
                return 200, json.dumps({
                    "errorcode": 0, "errmsg": "", "tables": ["bad", 3],
                }).encode()
            if "basic_data_service" in url:
                level = parsed["indipara"][0]["indiparams"][0]
                return 200, json.dumps(_basic_payload(
                    stub.basic_by_level[level])).encode()
            if "cmd_history_quotation" in url:
                return 200, json.dumps(_history_payload(
                    parsed["codes"].split(","), sessions[:20])).encode()
            raise AssertionError(f"未预置的申万端点: {url}")

        self._sw_post_patcher = patch.object(sw, "_http_api_curl_post",
                                             malformed_quotes)
        self._sw_post_patcher.start()
        self.addCleanup(self._sw_post_patcher.stop)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)  # 不是 500
        analysis = resp.json()["analysis"]
        intraday = analysis["background"]["intraday"]
        self.assertFalse(intraday["sector_found"])
        self.assertTrue(any("快照" in g for g in intraday["gaps"]))
        self.assertEqual(analysis["research_basis"], "sw2021_l1")

    def test_trend_source_not_claimed_eastmoney_when_unqualified(self):
        """申万趋势未取得（日线失败）：来源行不冒称东财口径。"""
        from datetime import datetime

        now = datetime(2026, 11, 19, 1, 35, 0)
        sessions, frozen_ctx = self._install_clock(self._svc, now=now)
        session_date = frozen_ctx.session_date.isoformat()
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        rows = []
        for (code, _n) in sw.SW_L1_DIRECTORY:
            ch = -0.867 if code == "801950.SL" else 1.0
            rows.append(_quote_row(code, session_date, ch))
        stub.quotes = rows
        stub.history_dates = []
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        trend = analysis["sector_trend"]
        self.assertIsNone(trend["source"])  # 未取得，如实 None
        md = resp.json()["markdown"]
        self.assertIn("未取得", md)
        self.assertNotIn("东财行业板块日线", md)  # 申万路由不冒称东财来源


class TestFallbackBudgetCutoffPersistence(SectorReportApiV99TestCase):
    """amend 三轮 API 持久化反例：东财兜底 core 阶段取数截止耗尽时，
    保留申万部分报告并存档/读回/可下载（总截止仍活 → 200 而非 504）。"""

    def test_em_core_budget_exhausted_keeps_sw_partial_archived(self):
        from unittest.mock import patch as _patch

        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "煤炭", "2": "煤炭开采", "3": "动力煤"}
        # 归属成功（申万主行业确认）；快照与日线失败 → 门槛触发
        stub.quotes = []
        stub.history_dates = []
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()

        def _exhausted_identify(code, *, budget):
            from src.services.sector_analysis_service import BudgetExhausted

            raise BudgetExhausted("取数预算已耗尽")

        with _patch.object(self._svc, "_identify_sector", _exhausted_identify):
            resp = self.client.post(
                "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
            )
        self.assertEqual(resp.status_code, 200, resp.text)  # 不是 504
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["research_basis"], "sw2021_l1")
        self.assertEqual(analysis["research_outcome"], "kept_sw_stage")
        self.assertEqual(analysis["identity"]["subject_status"], "confirmed")
        self.assertEqual(analysis["identity"]["board_code"], "801950.SL")
        self.assertTrue(any("核心阶段到达取数截止" in g for g in analysis["gaps"]))
        # 持久化：存档 + 详情读回 + 下载一致
        rows, total = self.db.list_sector_analysis_reports()
        self.assertEqual(total, 1)
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["analysis_json"]["research_basis"],
                         "sw2021_l1")
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown")
        self.assertEqual(download.status_code, 200)
        self.assertIn("申万 2021 一级行业", download.text)


class TestAttributionIdentityQualification(SectorReportApiV99TestCase):
    """amend 四：归属解析的精确身份/类型/冲突边界（经 generate 端点 +
    真实解析器；传输替身只回放受控 wire 形状）。"""

    def _generate_with_attribution(self, level_tables: Dict[str, Any]) -> Any:
        """level_payloads: level→tables；归属请求按层级单独出站。"""
        from datetime import datetime as _dt

        now = _dt(2026, 11, 19, 1, 35, 0)
        sessions, _frozen_ctx = self._install_clock(self._svc, now=now)
        stub = _SwTransportStub()
        stub.basic_by_level = {"1": "", "2": "", "3": ""}
        stub.quotes = []
        stub.history_dates = []
        self._install_sw_stub(stub)
        self._install_eastmoney_sentinels()

        def post(url, headers, body, *, label):
            parsed = json.loads(body.decode())
            stub.calls.append((url, parsed))
            if "basic_data_service" in url:
                level = parsed["indipara"][0]["indiparams"][0]
                payload = level_tables.get(level)
                if isinstance(payload, Exception):
                    raise payload
                return 200, json.dumps(payload).encode()
            if "real_time_quotation" in url:
                return 200, json.dumps(_quotes_payload([])).encode()
            if "cmd_history_quotation" in url:
                return 200, json.dumps(_history_payload(
                    parsed["codes"].split(","), sessions[:20])).encode()
            raise AssertionError(f"未预置的申万端点: {url}")

        self._sw_post_patcher = patch.object(sw, "_http_api_curl_post", post)
        self._sw_post_patcher.start()
        self.addCleanup(self._sw_post_patcher.stop)
        return self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600188&stock_name=兖矿能源"
        )

    @staticmethod
    def _table(code: str, value: Any) -> Dict[str, Any]:
        return {"thscode": code,
                "table": {"ths_the_sw_industry_stock": [value]}}

    def _patch_em_core_confirmed_no_recovery(self):
        """东财核心缝替身（主体确认但零核心行情恢复）：
        L1 拒绝场景必然触发兜底——让兜底走受控 core，验证申万降级理由
        有界随载荷可回查（不 500、不被吞）。"""
        core = {
            "ident": {
                "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
                "board_name": "白酒Ⅱ", "board_code": "BK1277",
                "subject_status": "confirmed",
                "membership": {"verified": True, "member_count": 20,
                               "verified_source": "cons_table"},
                "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
                "classification_system": "东财行业板块",
                "classification_level": None, "classification_version": None,
                "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
                "sector_name": "白酒Ⅱ", "gaps": [],
            },
            "board_snapshot": None, "cons_snapshot": None, "cons_observed_at": None,
            "cons_batch": None, "f10_members_source": False, "confirmed": True,
            "trend": {"eligible": True, "window_completed_sessions": 20,
                      "interval_count": 19, "start_date": None, "end_date": None,
                      "expected_dates": [], "qualified_sessions": None,
                      "observations": [], "period_return_pct": None,
                      "benchmark": None, "relative_return_pp": None,
                      "source": None, "observed_at": "2026-11-05T14:00:02",
                      "gaps": []},
            "prosper": {"sector_found": False, "momentum": None, "activity": None,
                        "amount_activity": None, "breadth": None, "rank_of": None,
                        "total_boards": None,
                        "row": {"board_name": "白酒Ⅱ", "board_code": "BK1277",
                                "change_pct": None, "turnover_rate": None,
                                "total_market_cap": None, "amount": None,
                                "up_count": None, "down_count": None,
                                "leader": "", "leader_change_pct": None},
                        "snapshot_source": None, "gaps": []},
        }
        return patch.object(self._svc, "_compose_eastmoney_core",
                            lambda code, *, budget: core)

    def test_wrong_thscode_table_not_accepted_exact_identity_honored(self):
        """错误代码的响应行不采用（不取首行）；同响应中的精确身份行被采用。"""
        resp = self._generate_with_attribution({
            "1": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("000001.SZ", "银行"),   # 错误代码行（旧实现会误取）
                self._table("600188.SH", "煤炭"),
            ]},
            "2": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", "煤炭开采")]},
            "3": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", "动力煤")]},
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        ident = resp.json()["analysis"]["identity"]
        self.assertEqual(ident["subject_status"], "confirmed")
        self.assertEqual(ident["board_code"], "801950.SL")  # 精确身份行的煤炭
        self.assertNotEqual(ident["board_name"], "银行")

    def test_conflicting_duplicate_values_rejected_not_first_wins(self):
        """同码多值冲突（煤炭 vs 煤炭开采）：不取其一 → 主行业不可确认。"""
        core_patch = self._patch_em_core_confirmed_no_recovery()
        core_patch.start()
        self.addCleanup(core_patch.stop)
        resp = self._generate_with_attribution({
            "1": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", "煤炭"),
                self._table("600188.SH", "煤炭开采"),
            ]},
            "2": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", "煤炭开采")]},
            "3": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", "动力煤")]},
        })
        self.assertEqual(resp.status_code, 200, resp.text)  # 不是 500
        payload = resp.json()["analysis"]
        gaps_text = " ".join(payload["gaps"])
        self.assertTrue(any("冲突" in g for g in payload["gaps"]), payload["gaps"])
        # 冲突消息只含计数，不含供应商返回值（防回显）
        self.assertNotIn("煤炭开采", gaps_text)
        self.assertNotIn("动力煤", gaps_text)

    def test_within_table_multiple_distinct_names_rejected(self):
        """同表内多个不同行业名（多值列表）：歧义冲突，不取首个。"""
        core_patch = self._patch_em_core_confirmed_no_recovery()
        core_patch.start()
        self.addCleanup(core_patch.stop)
        resp = self._generate_with_attribution({
            "1": {"errorcode": 0, "errmsg": "", "tables": [
                {"thscode": "600188.SH",
                 "table": {"ths_the_sw_industry_stock": ["煤炭", "煤炭开采"]}}]},
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()["analysis"]
        gaps_text = " ".join(payload["gaps"])
        self.assertTrue(any("同表多值冲突" in g for g in payload["gaps"]),
                        payload["gaps"])
        self.assertNotIn("煤炭开采", gaps_text)  # 供应商值不回显
        self.assertNotIn("煤炭开采", resp.json()["markdown"])

    def test_non_string_value_rejected_without_str_coercion(self):
        """非字符串值（数字）：不 str() 合法化 → 主行业不可确认（缺口可回查）。"""
        core_patch = self._patch_em_core_confirmed_no_recovery()
        core_patch.start()
        self.addCleanup(core_patch.stop)
        resp = self._generate_with_attribution({
            "1": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", 12345)]},
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()["analysis"]
        self.assertTrue(any("非字符串" in g for g in payload["gaps"]),
                        payload["gaps"])

    def test_over_long_name_rejected(self):
        """超 64 字符行业名：拒绝（schema 名称边界），不进入载荷。"""
        core_patch = self._patch_em_core_confirmed_no_recovery()
        core_patch.start()
        self.addCleanup(core_patch.stop)
        resp = self._generate_with_attribution({
            "1": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", "煤" * 65)]},
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()["analysis"]
        self.assertTrue(any("64 字符" in g for g in payload["gaps"]),
                        payload["gaps"])

    def test_bad_l2_l3_become_optional_gaps_never_500(self):
        """L1 合法 + L2/L3 值非法：确认保留 + 可选参考缺口，绝不 500。"""
        resp = self._generate_with_attribution({
            "1": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", "煤炭")]},
            "2": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", {"evil": "x" * 300})]},
            "3": {"errorcode": 0, "errmsg": "", "tables": [
                self._table("600188.SH", "煤" * 65)]},
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        ident = analysis["identity"]
        self.assertEqual(ident["subject_status"], "confirmed")
        self.assertEqual(ident["board_code"], "801950.SL")
        self.assertIsNone(ident["sw_chain"]["l2"])
        self.assertIsNone(ident["sw_chain"]["l3"])
        self.assertTrue(any("可选参考" in g for g in ident["gaps"]))
        self.assertNotIn("evil", resp.text)
