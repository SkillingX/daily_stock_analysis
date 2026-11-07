# -*- coding: utf-8 -*-
"""板块报告 v2 公开 API 闭环（issue #81 主测试缝）。

从公开板块 API 发起生成，经真实编排/资格/缺失逻辑、严格响应校验、
临时 DB/文件、历史及下载回读观察；仅隔离外部供应商（东财/巨潮/申万/iFinD
边界全部 mock，含巨潮兜底——第一轮审查发现全缺失用例会真实出网巨潮）。
不 mock 行业匹配、缺失语义、渲染/持久化风险。

测试基座隔离纪律（第一轮审查反例修正）：
- 临时 .env 不能覆盖继承的 process env：受控合法配置组合显式写入
  os.environ（``FINANCIAL_PRIMARY_SOURCE=ifind`` 与仅禁 ``ENABLE_IFIND``
  是 config 校验冲突的非法组合，显式 choice 全组合），全部改动保存并在
  tearDown 恢复；Config/DB 单例 reset。
- 意外出网守卫：任何 TCP 连接（AF_INET/AF_INET6）直接失败，离线回归
  中外部边界未隔离即红；本地 Unix socket 不受影响。

覆盖：正常主行业（含 v1 兼容投影）、名称近似不取首行、非 A 股拒绝、
供应商超时局部缺口、概念超限截取、渲染超预算不存档、同分钟并发原子
独占、超时不存档（晚到 IO 不采纳）、保存失败可见且不留记录/文件、
详情文件缺失明确错误、旧记录缺新字段可回看/下载、生成-详情-下载一致、
删除/重复删除反馈。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pandas as pd

try:
    # LiteLLM 导入期可能拉取价格表（意外出网守卫下的已知导入副作用）：
    # 用本地价格表开关避免离线回归被导入期请求拦截。
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
from src.storage import DatabaseManager


def _daily_hist_df(expected: list, base: float = 100.0, step: float = 1.0,
                   date_col: str = "日期", close_col: str = "收盘",
                   extra_rows: list[dict] | None = None) -> pd.DataFrame:
    """按期望交易日集构造升序日线（str 日期 + float 收盘，东财真实形状）。"""
    rows = []
    val = base
    for d in expected:
        rows.append({date_col: d.strftime("%Y-%m-%d"), close_col: val})
        val += step
    df = pd.DataFrame(rows)
    if extra_rows:
        df = pd.concat([df, pd.DataFrame(extra_rows)], ignore_index=True)
    return df


def _board_table(rows: list[tuple[str, str]]) -> pd.DataFrame:
    df = pd.DataFrame(
        {"板块名称": [r[0] for r in rows], "板块代码": [r[1] for r in rows],
         "涨跌幅": [1.65] * len(rows), "换手率": [2.3] * len(rows),
         "成交额": [3.1e9] * len(rows), "上涨家数": [18] * len(rows),
         "下跌家数": [2] * len(rows), "总市值": [1.9e12] * len(rows),
         "领涨股票": ["样本股"] * len(rows), "领涨股票-涨跌幅": [5.5] * len(rows)}
    )
    return df


def _cons_df(codes: list[str]) -> pd.DataFrame:
    """成分快照（形状参考东财真实返回：代码/名称/涨跌幅/成交额）。"""
    return pd.DataFrame({
        "代码": codes,
        "名称": [f"股票{c}" for c in codes],
        "涨跌幅": [1.0 + i * 0.1 for i in range(len(codes))],
        "成交额": [(len(codes) - i) * 1e8 for i in range(len(codes))],
    })


def _info_df(industry: str) -> pd.DataFrame:
    return pd.DataFrame({"item": ["行业"], "value": [industry]})


class SectorReportApiV2TestCase(unittest.TestCase):
    """临时 DB/报告目录 + 供应商边界与运行时环境完整隔离的公开 API 测试基座。"""

    REPORT_DIR_NAME = "reports/sector_analysis"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "sector_api_test.db"
        env_path = self.data_dir / ".env"
        env_path.write_text(
            "STOCK_LIST=600519\n"
            "ADMIN_AUTH_ENABLED=false\n"
            f"DATABASE_PATH={self.db_path}\n"
            "ENABLE_IFIND_MCP=false\n"
            "FINANCIAL_PRIMARY_SOURCE=choice\n"
            "DATA_SOURCE_PROFILE=choice\n"
            "SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS=300\n",
            encoding="utf-8",
        )
        # 受控合法配置组合：临时 .env 无法覆盖继承的 process env，显式写入
        # os.environ 并记录原值，tearDown 全量恢复（含 Config/DB 单例 reset）。
        self._env_backup: dict[str, Optional[str]] = {}
        self._controlled_env = {
            "ENV_FILE": str(env_path),
            "DATABASE_PATH": str(self.db_path),
            "ADMIN_AUTH_ENABLED": "false",
            "ENABLE_IFIND_MCP": "false",
            # FINANCIAL_PRIMARY_SOURCE=ifind 要求 ENABLE_IFIND_MCP=true（config
            # 校验冲突）：显式 choice 防继承 ifind 与禁用组合的非法组合。
            "FINANCIAL_PRIMARY_SOURCE": "choice",
            "DATA_SOURCE_PROFILE": "choice",
            "SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS": "300",
            # issue #83 合成模型配置（纯合成，无真实凭据；合成 wire 经
            # _install_research_success 只在需要的用例安装）：
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "LITELLM_MODEL": "openai/synthetic-standard",
            "OPENAI_API_KEYS": "synthetic-placeholder",
        }
        for name, value in self._controlled_env.items():
            self._env_backup[name] = os.environ.get(name)
            os.environ[name] = value
        Config.reset_instance()
        DatabaseManager.reset_instance()
        # 意外出网守卫：离线回归中任何 TCP 连接即失败（外部边界未隔离）。
        orig_connect = socket.socket.connect

        def _guarded_connect(sock, address):  # noqa: ANN001 - socket 签名透传
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError(f"离线回归意外出网: {address!r}")
            return orig_connect(sock, address)

        self._network_guard = patch.object(socket.socket, "connect", _guarded_connect)
        self._network_guard.start()
        self.addCleanup(self._network_guard.stop)
        app = create_app(static_dir=self.data_dir / "empty-static")
        self.client = TestClient(app)
        self.db = DatabaseManager.get_instance()
        # 报告目录指到临时目录（验收副作用只落临时目录）
        import src.services.sector_analysis_service as svc

        self._svc = svc
        self._orig_report_dir = svc._REPORT_DIR
        self.report_dir = self.data_dir / "reports" / "sector_analysis"

        def _fake_report_dir() -> Path:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            return self.report_dir

        svc._REPORT_DIR = self.report_dir
        self._report_dir_patcher = patch.object(svc, "get_report_dir", _fake_report_dir)
        self._report_dir_patcher.start()
        # 2026-11-07 收敛轮新增出站边界隔离：F10 声明/成员（datacenter）属
        # 独立外部边界——本基线用例的 clist/akshare 全链 mock 不覆盖它，统一
        # 关闭使身份链走既有 akshare mock 路径；F10 路径契约见
        # tests/test_sector_board_data_rebuild.py（真实 wire fixture 全链）。
        self._f10_isolation = patch.object(
            svc, "_fetch_board_claims", lambda code, budget: None,
        )
        self._f10_isolation.start()
        self.addCleanup(self._f10_isolation.stop)

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

    def _pooled_busy_timeout_ms(self) -> int:
        """从引擎池 checkout 一条连接读取当前 busy_timeout（观察连接级 PRAGMA）。"""
        with self.db._engine.connect() as conn:
            return int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one())

    # ---- issue #83 合成研究终端：搜索 provider 响应 + 模型 SSE wire ----
    # 只隔离终端供应商响应；真实 SearchService/准入/文章日期证据提取/证据
    # 登记表/引用与数字锚定校验/渲染/存档全部真实执行。

    def _install_research_success(self, *, results: Optional[list] = None,
                                  model_claims: Optional[list] = None,
                                  provider: Optional[object] = None):
        """注入合成搜索结果与合成模型响应（终端响应隔离）。

        - ``results``：合成 SearchResult 列表（真实 SearchService 兜住 provider
          fallback 与共享日期语义；provider 对象为受控合成，返回这些结果）。
        - ``provider``：显式传入真实 provider 对象（如 MiniMaxSearchProvider，
          配合对 ``_post_with_retry`` 的终端隔离——真实 raw 解析代码全链路），
          传入时忽略 ``results``。
        - ``model_claims``：合成模型 JSON 的 claims（经真实 LiteLLM 流处理 →
          真实 adapter/登记表/校验；``httpx.AsyncClient.send`` 返回合成 SSE，
          LiteLLM 官方流协议全链路真实）。
        """
        import json as _json
        from datetime import datetime as _datetime

        from src.search_service import SearchResult, get_search_service
        import src.services.sector_analysis_service as _svc

        today = _datetime.now().date().isoformat()
        if results is None:
            results = [{
                "title": "白酒行业新政策发布",
                "snippet": "监管部门发布白酒行业新规，行业迎来政策催化",
                "url": f"https://example.com/news/{today.replace('-', '')}/baijiu.html",
                "source": "example.com",
                "published_date": today,
            }]

        class _SyntheticProvider:
            """受控合成搜索 provider（终端响应隔离；search() 返回合成结果）。"""

            name = "SyntheticSearch"

            def __init__(self, rows: list[dict]):
                self._rows = rows

            @property
            def is_available(self) -> bool:
                return True

            def search(self, query: str, max_results: int = 5,
                       days: int = 7):
                return type("R", (), {
                    "query": query,
                    "results": [SearchResult(**row) for row in self._rows],
                    "provider": self.name,
                    "success": True,
                    "error_message": None,
                    "search_time": 0.01,
                })()

        search_svc = get_search_service()
        self._orig_providers = search_svc._providers
        search_svc._providers = [provider if provider is not None
                                 else _SyntheticProvider(results)]
        self.addCleanup(setattr, search_svc, "_providers", self._orig_providers)
        # 搜索单例在跨测试间保留：恢复原 providers（tearDown 的 env reset 已有）
        self.addCleanup(setattr, _svc, "_catalysts_last_call", None)

        if model_claims is None:
            model_claims = [{
                "kind": "interpretation",
                "text": "白酒行业新政策构成近期催化，目标板块在同业样本中位置居前",
                "evidence_refs": ["cat-1", "peers"],
            }]
        content = _json.dumps({"claims": model_claims}, ensure_ascii=False)

        import httpx

        orig_send = httpx.AsyncClient.send

        async def _synthetic_send(client_self, request, *args, **kwargs):
            # 只拦截模型出站：ASGI 传输（TestClient 应用请求）走真实链路
            if isinstance(client_self._transport, httpx.ASGITransport):
                return await orig_send(client_self, request, *args, **kwargs)
            chunks = [
                {"id": "synthetic", "object": "chat.completion.chunk", "created": 0,
                 "model": "synthetic-standard",
                 "choices": [{"index": 0, "delta": {"role": "assistant",
                                                    "content": content},
                              "finish_reason": None}]},
                {"id": "synthetic", "object": "chat.completion.chunk", "created": 0,
                 "model": "synthetic-standard",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            wire = "".join(
                "data: " + _json.dumps(c, ensure_ascii=False) + "\n\n"
                for c in chunks
            ) + "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=wire.encode(), request=request)

        self._model_send_patcher = patch.object(httpx.AsyncClient, "send",
                                                _synthetic_send)
        self._model_send_patcher.start()
        self.addCleanup(self._model_send_patcher.stop)

    # ---- 供应商边界隔离（形状参考 2026-11-05 实测 JSON；故障反例明确为假）----

    def _mock_vendors(self, *, industry: str | None, boards: list[tuple[str, str]] | None,
                      cons: list[str] | None, info_side_effect=None,
                      board_table: pd.DataFrame | None = None,
                      cninfo: str | None = None, info_delay: float = 0.0,
                      board_hist: pd.DataFrame | None = None,
                      bench_hist: pd.DataFrame | None = None,
                      cons_df: pd.DataFrame | None = None,
                      calendar_sessions: list | None | str = "real"):
        """隔离东财个股信息/巨潮兜底/板块全表/成分/趋势日线/申万链外部边界。

        ``board_table`` 传入时与身份共用同一快照对象（模拟快照复用路径）；
        ``boards`` 调用次数可经 ``self.board_calls`` 观察（同一快照不应二次出站）；
        ``info_delay`` 让个股信息接口真实延迟（预算钳制/晚到 IO 反例用）。

        issue #82 趋势边界：``board_hist``/``bench_hist`` 是按期望窗口构造的
        离线日线样本（真实返回形状），None = 供应商假故障；调用次数经
        ``self.hist_calls``/``self.bench_calls`` 观察。
        ``calendar_sessions="real"`` 用真实严格日历（本地 xcals 数据）；
        传 list 固定期望窗口（构造样本用），传 None 模拟严格日历不可用。
        """
        import time as _time

        import akshare as ak

        import src.services.sector_analysis_service as svc

        self.board_calls: list[int] = []
        self.hist_calls: list[dict] = []
        self.bench_calls: list[dict] = []

        def _info(symbol):  # noqa: ARG001
            if info_delay:
                _time.sleep(info_delay)
            if info_side_effect is not None:
                raise info_side_effect
            if industry is None:
                raise RuntimeError("em individual down (fake)")
            return _info_df(industry)

        def _boards():
            self.board_calls.append(1)
            data = board_table if board_table is not None else (
                _board_table(boards) if boards is not None else None)
            if data is None:
                raise RuntimeError("em boards down (fake)")
            return data

        def _cons(symbol):  # noqa: ARG001
            if cons_df is not None:
                return cons_df
            if cons is None:
                raise RuntimeError("em cons down (fake)")
            return _cons_df(cons)

        def _cninfo(symbol):  # noqa: ARG001
            if cninfo is not None:
                return pd.DataFrame({"所属行业": [cninfo], "主营业务": [""]})
            raise RuntimeError("cninfo down (fake)")

        def _board_hist(**kwargs):
            self.hist_calls.append(dict(kwargs))
            if board_hist is None:
                raise RuntimeError("em board hist down (fake)")
            return board_hist

        def _bench_hist(**kwargs):
            self.bench_calls.append(dict(kwargs))
            if bench_hist is None:
                raise RuntimeError("em index daily down (fake)")
            return bench_hist

        if calendar_sessions == "real":
            cal_patch = None
        else:
            cal_patch = patch.object(
                svc, "recent_completed_trading_sessions",
                lambda *a, **k: list(calendar_sessions) if calendar_sessions else None,
            )
            cal_patch.start()
            self.addCleanup(cal_patch.stop)

        patches = [
            patch.object(ak, "stock_individual_info_em", _info),
            patch.object(ak, "stock_profile_cninfo", _cninfo),
            patch.object(ak, "stock_board_industry_name_em", _boards),
            patch.object(ak, "stock_board_industry_cons_em", _cons),
            patch.object(ak, "stock_board_industry_hist_em", _board_hist),
            patch.object(ak, "stock_zh_index_daily_em", _bench_hist),
            # 申万参考链（外部边界）隔离：返回固定参考链，缺口如实透出
            patch.object(
                svc, "_resolve_sw_chain",
                lambda code, text, budget: {
                    "l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                    "l3_code": "851251.SI", "gaps": [],
                },
            ),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])


class TestGenerateClosedLoop(SectorReportApiV2TestCase):
    """生成 → 严格响应 → 存档 → 历史/详情/下载回读闭环。"""

    def test_generate_success_full_loop(self):
        # issue #83：完整闭环含催化+综合成功（合成终端，准入/校验真实）
        self._install_research_success()
        # 期望窗口取真实严格日历（本地 xcals 数据，离线）；样本按真实返回形状构造
        sessions = recent_completed_trading_sessions("cn", 20)
        expected = sorted(sessions)
        board_hist = _daily_hist_df(expected, base=1000.0, step=1.0)
        bench_hist = _daily_hist_df(expected, base=4000.0, step=1.0,
                                    date_col="date", close_col="close")
        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        # 成分含目标股但不在成交额前五 → 样本 6 家（板块内仅 2 只时按实际范围）
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
                           cons=["600519", "000568"], board_table=boards,
                           board_hist=board_hist, bench_hist=bench_hist)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["report_id"].startswith("sa_"))
        analysis = body["analysis"]
        self.assertEqual(analysis["payload_version"], 2)
        self.assertEqual(analysis["identity"]["subject_status"], "confirmed")
        self.assertEqual(analysis["identity"]["board_code"], "BK1277")
        # 分类元数据：体系锁定，层级/版本供应商未返回如实未知
        self.assertEqual(analysis["identity"]["classification_system"], "东财行业板块")
        self.assertIsNone(analysis["identity"]["classification_level"])
        self.assertIsNone(analysis["identity"]["classification_version"])
        self.assertEqual(analysis["assessment"]["status"], "unscored")
        self.assertEqual(analysis["assessment"]["completeness"], "complete")
        self.assertIsNone(analysis["assessment"]["score"])
        # ---- issue #82 趋势块：严格日历窗口 + 20/20 合格 + 区间收益与相对差 ----
        trend = analysis["sector_trend"]
        self.assertTrue(trend["eligible"])
        self.assertEqual(trend["window_completed_sessions"], 20)
        self.assertEqual(trend["interval_count"], 19)
        self.assertEqual(trend["start_date"], expected[0].isoformat())
        self.assertEqual(trend["end_date"], expected[-1].isoformat())
        self.assertEqual(len(trend["expected_dates"]), 20)
        self.assertIsNotNone(trend["period_return_pct"])
        self.assertIsNotNone(trend["relative_return_pp"])
        self.assertEqual(trend["benchmark"]["qualified_sessions"], 20)
        self.assertIsNotNone(trend["benchmark"]["period_return_pct"])
        self.assertEqual(trend["gaps"], [])
        # ---- issue #82 rework：实际 20 日日线进载荷并持久化（无发明值）----
        self.assertEqual(trend["qualified_sessions"], 20)
        self.assertEqual(len(trend["observations"]), 20)
        self.assertEqual(trend["benchmark"]["observations"][0]["date"],
                         expected[0].isoformat())
        # 板块观测收盘与 mock 日线逐日一致（升序、真实值）
        for i, obs in enumerate(trend["observations"]):
            self.assertEqual(obs["date"], expected[i].isoformat())
            self.assertEqual(obs["close"], 1000.0 + i)
        self.assertEqual(len(trend["benchmark"]["observations"]), 20)
        # 出站窗口参数与期望窗口一致（BK 代码直查）
        self.assertEqual(len(self.hist_calls), 1)
        self.assertEqual(self.hist_calls[0]["symbol"], "BK1277")
        self.assertEqual(self.hist_calls[0]["start_date"], expected[0].strftime("%Y%m%d"))
        self.assertEqual(self.hist_calls[0]["end_date"], expected[-1].strftime("%Y%m%d"))
        # ---- issue #82 同业块：同一成分快照选取（无第二次成分出站）----
        peers = analysis["sector_peers"]
        self.assertTrue(peers["eligible"])
        self.assertEqual(peers["sample_size"], 2)  # 快照只有 2 只：如实 2 家
        codes = [i["code"] for i in peers["items"]]
        self.assertIn("600519", codes)
        target_items = [i for i in peers["items"] if i["is_target"]]
        self.assertEqual(len(target_items), 1)
        self.assertIsNotNone(peers["target_change_pct_rank"])
        self.assertTrue(peers["gaps"])  # 样本不足 5 家如实缺口
        # v1 兼容投影：非评分事实路径可读，评分字段显式退役 None
        self.assertEqual(analysis["identification"]["industry_em"], "白酒Ⅱ")
        self.assertIn(analysis["policy"]["lean"], ("supportive", "neutral", "restrictive"))
        self.assertIsNone(analysis["policy"]["score"])  # 政策档位分退役
        self.assertIsNotNone(analysis["prosperity"]["momentum"])  # 事实投影可读
        self.assertIsNone(analysis["prosperity"]["capital"])  # 旧「资金」维度退役
        self.assertEqual(analysis["base_rate"]["is_default"], False)
        for retired_key in ("sector_score", "band", "beta_meaning", "weights",
                            "prosperity_score"):
            self.assertIn(retired_key, analysis)  # 追加兼容：路径仍在
            self.assertIsNone(analysis[retired_key])  # 退役语义：显式 None
        md = body["markdown"]
        self.assertIn("BK1277", md)
        self.assertIn("未评分", md)
        self.assertIn("完整性", md)
        self.assertNotIn("综合板块得分", md)  # 旧综合分不再出现在新报告正文
        self.assertNotIn("60分中性", md)
        self.assertIn("概念入口未启用", md)  # 概念停用缺口如实透出
        self.assertIn("申万", md)  # 申万参考链单列披露
        # ---- issue #82 rework：实际日线观测表进正文（20 行 + 覆盖）----
        self.assertIn("板块合格观测：20/20", md)
        self.assertIn("| 日期 | 板块收盘（板块指数点位） |", md)
        self.assertIn(f"| {expected[0].isoformat()} | 1000.0 |", md)
        self.assertIn("| 日期 | 沪深 300 收盘（指数点位） |", md)  # 渲染后盘古空格
        self.assertIn(f"| {expected[-1].isoformat()} | 4019.0 |", md)
        self.assertIn("当日成交额（元）", md)  # 成交额带单位
        # 身份与行情同一快照：板块全表只出站一次
        self.assertEqual(len(self.board_calls), 1)

        # 历史/详情/下载回读一致
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 1)
        self.assertIsNone(lst["data"][0]["sector_score"])  # 新记录不写旧综合分
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        d = detail.json()["data"]
        self.assertEqual(d["markdown"], md)  # 详情与生成响应一致
        self.assertEqual(d["analysis_json"]["payload_version"], 2)
        download = self.client.get(f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.text, md)  # 下载读存档原文
        self.assertIn("attachment", download.headers.get("content-disposition", ""))

    def test_generate_rejects_non_a_share(self):
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=00700&stock_name=腾讯"
        )
        self.assertEqual(resp.status_code, 400)

    def test_near_name_board_not_locked_insufficient(self):
        """名称近似：不取首行，身份 ambiguous → 资料不足贯穿载荷/正文。"""
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒", "BK0001"), ("啤酒", "BK0002")],
                           cons=["600519"])
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["identity"]["subject_status"], "ambiguous")
        self.assertEqual(analysis["assessment"]["status"], "insufficient_data")
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        self.assertTrue(analysis["assessment"]["reasons"])
        self.assertIn("近似", resp.json()["markdown"])
        # 部分报告仍存档（如实标注资料不足），历史可回看
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 1)

    def test_unverified_membership_partial_report(self):
        """成分不含目标：unverified + 缺口贯穿，不静默确证。"""
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")],
                           cons=["000568", "000596"])
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["identity"]["subject_status"], "unverified")
        self.assertEqual(analysis["assessment"]["status"], "unscored")
        self.assertTrue(any("归属未确认" in g for g in analysis["gaps"]))
        self.assertIn("归属未确认", resp.json()["markdown"])

    def test_missing_everything_default_base_rate_no_neutral(self):
        """固定反例：全缺失 + 默认基率 0.5 → 资料不足，无 60 分中性。

        巨潮兜底同样隔离（第一轮审查发现本用例曾真实出网巨潮）。
        """
        self._mock_vendors(industry=None, boards=None, cons=None)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["assessment"]["status"], "insufficient_data")
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        self.assertIsNone(analysis["assessment"]["score"])
        md = resp.json()["markdown"]
        self.assertIn("资料不足", md)
        # 旧分档词汇（顺风/偏顺风/中性/逆风/板块β）不再出现在任何正文结论里
        for legacy_band_word in ("顺风", "逆风", "板块β"):
            self.assertNotIn(legacy_band_word, md)

    def test_vendor_timeout_keeps_partial_report_not_budget_error(self):
        """供应商自身 TimeoutError：局部缺口保留有效内容，不谎报预算耗尽 504。

        第一轮审查反例：Py3.11 futures.TimeoutError 即内建 TimeoutError，
        初版把 fn 内部的供应商超时终结为预算耗尽，丢弃全部有效事实。
        """
        self._mock_vendors(industry=None, boards=None, cons=None,
                           info_side_effect=TimeoutError("vendor socket timeout (fake)"))
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)  # 不 504
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["assessment"]["status"], "insufficient_data")
        self.assertTrue(
            any("TimeoutError" in g or "timeout" in g.lower() for g in analysis["gaps"]),
            analysis["gaps"],
        )
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 1)  # 部分报告存档可回看

    def test_concepts_over_display_cap_partial_report_saved(self):
        """51 条合法概念：截取展示上限并标注总量，部分报告正常存档（不 500）。

        第一轮审查反例：初版 schema 拒绝 51 条 → 写库后才在 API 500，
        留下与终态不一致的存档。
        """
        from data_provider.ifind_fundamental_adapter import ConceptMembership, IfindFetcher

        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")], cons=["600519"])
        os.environ["ENABLE_IFIND_MCP"] = "true"  # setUp 已备份原值
        # 修改前捕获真实旧值（含继承的 process env），tearDown 全量恢复——
        # 曾用 _env_backup.setdefault(name, None) 把继承值记成 None，
        # tearDown 会误删调用方环境（第二轮审查 sentinel 反例）。
        for name, value in (("IFIND_MCP_ENDPOINT", "http://127.0.0.1:9"),
                            ("IFIND_MCP_TOKEN", "unused-in-mock")):
            self._env_backup[name] = os.environ.get(name)
            os.environ[name] = value
        Config.reset_instance()
        membership = ConceptMembership(
            code="600519", concepts=[f"概念{i}" for i in range(51)],
            source="ifind", as_of_nature="latest", errors=[],
        )
        patcher = patch.object(
            IfindFetcher, "concept_membership",
            lambda self, code, *, deadline=None: membership,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        try:
            resp = self.client.post(
                "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
            )
        finally:
            os.environ["ENABLE_IFIND_MCP"] = "false"
            Config.reset_instance()
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["identity"]["subject_status"], "confirmed")
        concepts = analysis["concepts"]
        self.assertEqual(len(concepts["items"]), 50)  # 展示上限截取
        self.assertEqual(concepts["total_returned"], 51)  # 原始总量如实透出
        self.assertTrue(concepts["truncated"])
        self.assertTrue(any("展示截取" in g for g in concepts["gaps"]))
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 1)  # 部分报告已存档且终态一致
        rid = resp.json()["report_id"]
        download = self.client.get(f"/api/v1/sector-analysis/reports/{rid}/markdown")
        self.assertEqual(download.status_code, 200)

    def test_render_overruns_budget_no_archive(self):
        """渲染/收尾超主截止：不保存伪成功（初版 1 秒预算 1.32 秒仍 success）。"""
        import src.services.sector_analysis_service as svc

        os.environ["SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS"] = "1.5"
        Config.reset_instance()
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")], cons=["600519"])
        # 收尾余量压缩为 0.5s（patch 常量）：取数预算 1s，渲染注入 2.5s
        # sleep → 渲染后主截止已过 → 保存前检查拒绝，不存档。
        orig_render = svc._render

        def _slow_render(*args, **kwargs):  # noqa: ANN001
            import time

            time.sleep(2.5)
            return orig_render(*args, **kwargs)

        with patch.object(svc, "_FINISH_MARGIN_SECONDS", 0.5), \
                patch.object(svc, "_render", _slow_render):
            resp = self.client.post(
                "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
            )
        self.assertEqual(resp.status_code, 504, resp.text)
        self.assertIn("超时", resp.json()["message"])
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 0)
        self.assertFalse(self.report_dir.exists() and any(self.report_dir.iterdir()))

    def test_save_failure_visible_no_record_no_orphan_file(self):
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")], cons=["600519"])
        with patch.object(DatabaseManager.get_instance(), "save_sector_analysis_report",
                          lambda record, deadline=None: False):
            resp = self.client.post(
                "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
            )
        self.assertEqual(resp.status_code, 500, resp.text)
        self.assertIn("保存失败", resp.json()["message"])
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 0)
        self.assertFalse(self.report_dir.exists() and any(self.report_dir.iterdir()))

    def test_concurrent_same_minute_requests_own_files(self):
        """同分钟并发：原子独占 ID/文件所有权，两请求各自成功互不破坏。

        第一轮审查反例（review-concurrent-save.py）：先 exists 后 write +
        失败无条件 unlink → 两请求 200/500、成功报告文件被删、下载 404。
        """
        import src.services.sector_analysis_service as svc

        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")], cons=["600519"])
        barrier = threading.Barrier(2)
        orig_claim = svc._claim_report_file

        def _claim_with_barrier(base):
            barrier.wait(timeout=8)
            return orig_claim(base)

        results: list = []

        def _request(_):
            results.append(self.client.post(
                "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=样本股"
            ))

        with patch.object(svc, "_claim_report_file", _claim_with_barrier):
            threads = [threading.Thread(target=_request, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        self.assertEqual([r.status_code for r in results], [200, 200],
                         [r.text for r in results])
        rid_a, rid_b = results[0].json()["report_id"], results[1].json()["report_id"]
        self.assertNotEqual(rid_a, rid_b)  # 原子独占：不同 ID
        for rid in (rid_a, rid_b):
            self.assertTrue((self.report_dir / f"{rid}.md").exists())
            self.assertEqual(
                self.client.get(f"/api/v1/sector-analysis/reports/{rid}/markdown").status_code,
                200,
            )
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 2)

    def test_detail_missing_file_explicit_error(self):
        """存档文件缺失：详情明确报错，不吞异常返回 200 空正文。"""
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")], cons=["600519"])
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        rid = resp.json()["report_id"]
        (self.report_dir / f"{rid}.md").unlink()
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 500)
        self.assertIn("缺失", detail.json()["message"])  # 项目错误信封 {error, message, detail}
        self.assertTrue(detail.json()["message"].strip())


class TestIntradayIdentityBinding(SectorReportApiV2TestCase):
    """行情行命中必须与身份锁定同键唯一（名称+代码），同键冲突不取首行。

    第二轮审查真实 API 反例：同快照 [错误行业/BK1277/99%, 白酒Ⅱ/BK1277/1.65%]
    时身份确认白酒Ⅱ，行情却按代码 iloc[0] 绑定错误行业 99%，gaps=[] 伪完整存档。
    """

    def test_duplicate_code_binds_same_key_row(self):
        """同代码两行：只允许 名称+代码 同键唯一命中，不 iloc[0]。"""
        table = _board_table([("错误行业", "BK1277"), ("白酒Ⅱ", "BK1277")])
        table.loc[0, "涨跌幅"] = 99.0
        self._mock_vendors(industry="白酒Ⅱ", boards=None, cons=["600519"],
                           board_table=table)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        a = resp.json()["analysis"]
        self.assertEqual(a["identity"]["subject_status"], "confirmed")
        intraday = a["background"]["intraday"]
        self.assertTrue(intraday["sector_found"])
        self.assertEqual(intraday["row"]["board_name"], "白酒Ⅱ")  # 同键行，非首行
        self.assertEqual(intraday["row"]["board_code"], "BK1277")
        self.assertEqual(intraday["row"]["change_pct"], 1.65)
        self.assertIsNotNone(intraday["momentum"])
        self.assertEqual(intraday["gaps"], [])

    def test_same_code_wrong_name_not_silently_selected(self):
        """代码命中但名称与锁定名不一致：不静默取首行（不回退其他板块）。"""
        table = _board_table([("错误行业", "BK1277")])
        self._mock_vendors(industry="白酒Ⅱ", boards=None, cons=["600519"],
                           board_table=table)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        a = resp.json()["analysis"]
        # 身份阶段：板块表无「白酒Ⅱ」精确命中 → 无法锁定
        self.assertEqual(a["identity"]["subject_status"], "ambiguous")
        self.assertEqual(a["assessment"]["status"], "insufficient_data")
        self.assertFalse(a["background"]["intraday"]["sector_found"])

    def test_duplicate_same_key_rows_not_locked(self):
        """同名称+同代码重复两行：同键多行不可锁定，不取首行。"""
        table = _board_table([("白酒Ⅱ", "BK1277"), ("白酒Ⅱ", "BK1277")])
        self._mock_vendors(industry="白酒Ⅱ", boards=None, cons=["600519"],
                           board_table=table)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        a = resp.json()["analysis"]
        self.assertEqual(a["identity"]["subject_status"], "ambiguous")
        intraday = a["background"]["intraday"]
        self.assertFalse(intraday["sector_found"])
        self.assertTrue(any("无法唯一锁定" in g for g in intraday["gaps"]))


class TestProvenanceRepairApiV2(SectorReportApiV2TestCase):
    """provenance repair 缺口的公开 API 回归（合成终端；准入/严格响应/渲染/
    存档/详情/下载全链真实）：显式倍数词拒绝、候选文本类型契约、终态 URL
    provenance（原/终态引用一致读回）。"""

    _GENERATE = ("/api/v1/sector-analysis/generate"
                 "?stock_code=600519&stock_name=贵州茅台")

    def _install_vendors(self):
        """确认身份 + 趋势/同业正常样本（聚焦催化/综合缺口的公开 API 行为）。"""
        sessions = recent_completed_trading_sessions("cn", 20)
        expected = sorted(sessions)
        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ",
            boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"], board_table=boards,
            board_hist=_daily_hist_df(expected, base=1000.0, step=1.0),
            bench_hist=_daily_hist_df(expected, base=4000.0, step=1.0,
                                      date_col="date", close_col="close"),
        )

    def _install_terminal_probe(self, by_url):
        """隔离文章探查终端（URL → (evidence, failure, final_url) 合成）；
        准入/资格/登记/校验/渲染/存档全部真实。"""
        import src.services.sector_analysis_service as _svc

        def _fake_probe(url, budget):  # noqa: ANN001
            return by_url[url]

        patcher = patch.object(_svc, "_probe_article_date_evidence", _fake_probe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _meta_evidence(self, declared_url, meta_date):
        from src.services import sector_research as _sr

        html = (
            '<!DOCTYPE html><html><head>'
            '<meta property="article:published_time" content="'
            + meta_date.isoformat() + 'T09:00:00">'
            "<title>白酒行业新政策</title></head><body><p>正文</p></body></html>"
        )
        return _sr.extract_article_date_evidence(html, declared_url)

    def test_doubling_word_claim_rejected_partial_api(self):
        # 缺口 1（公开 API）：模型输出含「企业利润将翻倍」（显式利润 ×2 声明、
        # 材料无利润数字）→ 整条拒绝、rejected_count=1、完整性 partial；
        # 正常 claim 与全部服务端事实保留；Markdown 显式拒绝计数
        self._install_research_success(model_claims=[
            {"kind": "interpretation",
             "text": "白酒行业新政策构成近期催化，目标板块在同业样本中位置居前",
             "evidence_refs": ["cat-1", "peers"]},
            {"kind": "interpretation", "text": "企业利润将翻倍",
             "evidence_refs": ["cat-1"]},
        ])
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        a = resp.json()["analysis"]
        syn = a["sector_synthesis"]
        self.assertEqual(syn["status"], "ok")
        self.assertEqual(syn["rejected_count"], 1)
        self.assertEqual(a["assessment"]["completeness"], "partial")
        self.assertTrue(
            any("已拒绝" in r for r in a["assessment"]["reasons"]),
            a["assessment"]["reasons"],
        )
        self.assertEqual(len(syn["claims"]), 1)  # 正常 claim 保留
        self.assertNotIn("翻倍", syn["summary"] or "")  # 摘要不含模型文本
        md = resp.json()["markdown"]
        self.assertIn("1 条结论未通过定性/引用/结构校验", md)
        self.assertIn("完整性为**部分**", md)

    def test_malformed_title_type_counted_valid_row_kept_api(self):
        # 缺口 4（公开 API）：真实供应商 JSON title=dict 曾被 str() 字符串化
        # 成假标题登记 cat-1 → 单一准入入口类型契约：畸形行本地计数
        # （dropped_invalid_field=1），剩余有效行照常准入；报告正常存档
        self._install_research_success(results=[
            {"title": {"n": "假标题"}, "snippet": "白酒行业动态",
             "url": "https://example.com/news/bad.html", "source": "s",
             "published_date": None},
            {
                "title": "白酒行业新政策发布",
                "snippet": "监管部门发布白酒行业新规",
                "url": "https://example.com/news/good.html",
                "source": "example.com",
                "published_date": datetime.now().date().isoformat(),
            },
        ])
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        a = resp.json()["analysis"]
        cat = a["sector_catalysts"]
        self.assertEqual(cat["dropped_invalid_field"], 1)
        self.assertEqual(len(cat["items"]), 1)
        self.assertEqual(cat["items"][0]["url"],
                         "https://example.com/news/good.html")
        self.assertEqual(a["assessment"]["completeness"], "complete")
        md = resp.json()["markdown"]
        self.assertIn("字段无效 1", md)

    def test_redirect_terminal_conflict_stays_background_api(self):
        # 缺口 2（公开 API 反例）：original.html→302→/2020/11/06/ 终态→
        # 200 meta=今天。终态 URL 声明参与冲突判定 → provenance 冲突保守
        # 背景（不注册 cat-1、不伪 complete）；背景保留安全原因。
        # source-contract convergence：背景**保留终态 provenance**——
        # final_url=实际文章地址、source_domain=终态 host（url 保留原引用），
        # 生成/详情一致、Markdown 显示实际终态地址
        import src.services.sector_analysis_service as _svc

        original = "https://news.example.com/original.html"
        terminal = "https://publisher.example.net/2020/11/06/final.html"
        ev = self._meta_evidence(terminal, datetime.now().date())
        self._install_research_success(results=[{
            "title": "白酒行业新政策发布", "snippet": "新规落地",
            "url": original, "source": "news.example.com",
            "published_date": None,
        }])
        self._install_terminal_probe({original: (ev, None, terminal)})
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        a = body["analysis"]
        cat = a["sector_catalysts"]
        self.assertEqual(cat["items"], [])
        self.assertEqual(len(cat["background_items"]), 1)
        bg = cat["background_items"][0]
        reasons = bg.get("downgrade_reason") or ""
        self.assertIn("冲突", reasons)
        # 背景终态 provenance 保留（原引用 + 实际文章地址 + 实际域名）
        self.assertEqual(bg["url"], original)
        self.assertEqual(bg["final_url"], terminal)
        self.assertEqual(bg["source_domain"], "publisher.example.net")
        self.assertEqual(a["assessment"]["completeness"], "partial")
        md = body["markdown"]
        self.assertIn("实际文章终态地址", md)
        self.assertIn(terminal, md)
        # 生成/详情/下载读回一致（背景 provenance 不丢失）
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_cat = detail.json()["data"]["analysis_json"]["sector_catalysts"]
        self.assertEqual(detail_cat["background_items"],
                         cat["background_items"])
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.text, md)

    def test_long_distinct_hostnames_full_identity_api(self):
        # 缺口 1（公开 API）：两个合法 131 字符 hostname（共享前 120、仅
        # one/two 标签不同）独立注册——完整身份进载荷/存档/Markdown，
        # 不被截断伪同域合并（root standards 反例：曾 1 cat + false
        # dropped_syndicated=1 + 存档域名是不存在的截断串）
        prefix = "a" * 60 + "." + "b" * 58
        host_one = f"{prefix}.one.example"
        host_two = f"{prefix}.two.example"
        today = datetime.now().date().isoformat()
        self._install_research_success(results=[
            {"title": "白酒行业新政策发布", "snippet": "新规落地",
             "url": f"https://{host_one}/a", "source": host_one,
             "published_date": today},
            {"title": "白酒行业新政策发布", "snippet": "新规落地",
             "url": f"https://{host_two}/b", "source": host_two,
             "published_date": today},
        ])
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        a = resp.json()["analysis"]
        cat = a["sector_catalysts"]
        self.assertEqual(len(cat["items"]), 2)
        self.assertEqual({i["source_domain"] for i in cat["items"]},
                         {host_one, host_two})
        self.assertEqual(cat["dropped_syndicated"], 0)
        md = resp.json()["markdown"]
        self.assertIn(host_one, md)
        self.assertIn(host_two, md)

    def test_minimax_malformed_rows_between_good_rows_public_api(self):
        # 缺口 3（公开 POST + 真实 MiniMax raw 解析）：raw rows=[good,
        # snippet-object, good] 曾在切片处 TypeError → 整批空失败、两条
        # 有效新闻全丢、invalid_field=0；契约下真实解析保留两条 good 行、
        # malformed_rows=1 经结构化字段聚进 dropped_invalid_field（供应商
        # 侧 + 本地准入 = 诚实总数，无双重计数），报告正常 complete 存档
        import src.search_service as _ss
        from src.search_service import MiniMaxSearchProvider

        today = datetime.now().date().isoformat()
        organic = [
            {"title": "白酒行业新政策发布", "snippet": "新规落地",
             "link": "https://example.com/news/a.html", "date": today},
            {"title": "白酒行业动态", "snippet": {"s": 1},
             "link": "https://example.com/news/bad.html", "date": today},
            {"title": "白酒行业新政策发布", "snippet": "新规落地",
             "link": "https://example.com/news/c.html", "date": today},
        ]

        class _SyntheticHTTP200:
            status_code = 200
            headers = {"content-type": "application/json"}

            def json(self):
                return {"base_resp": {"status_code": 0}, "organic": organic}

        provider = MiniMaxSearchProvider(["synthetic-key"])
        self._install_research_success(provider=provider)
        post_patcher = patch.object(
            _ss, "_post_with_retry",
            lambda *a, **k: _SyntheticHTTP200(),
        )
        post_patcher.start()
        self.addCleanup(post_patcher.stop)
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        a = resp.json()["analysis"]
        cat = a["sector_catalysts"]
        # 两条有效行保留（同题同域第二条按同域同题判重——这是既有去重
        # 语义，不是丢失；关键在它们都**到达了准入**）
        self.assertEqual(len(cat["items"]) + cat["dropped_syndicated"]
                         + cat["dropped_duplicate_url"], 2)
        # 供应商畸形行结构化计数聚进 invalid_field（本地拒绝为 0——
        # 好行类型全部有效）
        self.assertEqual(cat["dropped_invalid_field"], 1)
        self.assertEqual(cat["provider"], "MiniMax")
        md = resp.json()["markdown"]
        self.assertIn("字段无效 1", md)

    def test_redirect_terminal_consistent_upgrades_final_url_readback(
            self):
        # 缺口 2（公开 API 正控 + 读回一致）：一致日期重定向照常升级
        # article_metadata；payload 携带 final_url（终态）且 url 保留原引用
        # 身份；Markdown 标注实际终态地址；生成/详情/下载三入口逐字一致
        original = "https://alias.example.com/redirect.html"
        today = datetime.now().date()
        terminal = (f"https://publisher.example.com/dated/"
                    f"{today.isoformat()}/story.html")
        ev = self._meta_evidence(terminal, today)
        self._install_research_success(results=[{
            "title": "白酒行业新政策发布", "snippet": "新规落地",
            "url": original, "source": "alias.example.com",
            "published_date": None,
        }])
        self._install_terminal_probe({original: (ev, None, terminal)})
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        a = body["analysis"]
        cat = a["sector_catalysts"]
        self.assertEqual(len(cat["items"]), 1)
        item = cat["items"][0]
        self.assertEqual(item["date_basis"], "article_metadata")
        self.assertEqual(item["url"], original)  # 原引用身份保留
        self.assertEqual(item["final_url"], terminal)  # 实际文章地址如实
        self.assertEqual(item["source_domain"], "publisher.example.com")
        md = body["markdown"]
        self.assertIn("日期证据实际终态地址", md)
        self.assertIn(terminal, md)
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["markdown"], md)
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.text, md)

    def test_distinct_ipv6_hosts_both_kept_api(self):
        # 缺口 3（公开 API）：两个不同公网 IPv6 literal host、同题、无转载
        # 标记 → 按解析 hostname 独立注册（不按 '[2606' 伪同域合并）
        today = datetime.now().date().isoformat()
        self._install_research_success(results=[
            {"title": "白酒行业新政策发布", "snippet": "新规落地",
             "url": "https://[2606:4700:4700::1111]/a", "source": "v6a",
             "published_date": today},
            {"title": "白酒行业新政策发布", "snippet": "新规落地",
             "url": "https://[2606:4700:4700::1001]/b", "source": "v6b",
             "published_date": today},
        ])
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        cat = resp.json()["analysis"]["sector_catalysts"]
        self.assertEqual(len(cat["items"]), 2)
        domains = {i["source_domain"] for i in cat["items"]}
        self.assertEqual(domains, {"2606:4700:4700::1111",
                                   "2606:4700:4700::1001"})
        self.assertEqual(cat["dropped_syndicated"], 0)

    def _citations_readback(self, body):
        """生成 Markdown = 详情 Markdown = 下载原文（引用三入口一致）。"""
        md = body["markdown"]
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["markdown"], md)
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.text, md)
        return md

    def test_original_citations_visible_provider_and_article_no_redirect(
            self):
        # report-citations P2 反例：正常 provider-dated 与非重定向
        # article-dated 报告 complete，但页面/下载没有任何文章引用——
        # JSON 有 url 而渲染从不输出 c.url。模板最小修复：标题链接至
        # 原始引用（两条日期路径一致），读回三入口逐字一致
        provider_url = "https://news.example.com/provider.html"
        article_url = "https://news.example.com/article-no-redirect.html"
        today = datetime.now().date()
        ev = self._meta_evidence(article_url, today)  # 无重定向：终态==原 URL
        self._install_research_success(results=[
            {"title": "白酒行业新政策发布", "snippet": "新规落地",
             "url": provider_url, "source": "news.example.com",
             "published_date": today.isoformat()},
            {"title": "白酒行业板块动态", "snippet": "新规解读",
             "url": article_url, "source": "news.example.com",
             "published_date": None},
        ])
        self._install_terminal_probe({article_url: (ev, None, article_url)})
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        cat = body["analysis"]["sector_catalysts"]
        # provider 行 + article 行（无重定向 final_url==url）都到达准入；
        # 两条标题不同，各自独立注册
        self.assertEqual(len(cat["items"]), 2)
        self.assertEqual(body["analysis"]["assessment"]["completeness"],
                         "complete")
        md = self._citations_readback(body)
        # 原始引用以可用 Markdown 链接可见（两条日期路径一致）
        self.assertIn("[白酒行业新政策发布](" + provider_url + ")", md)
        self.assertIn("[白酒行业板块动态](" + article_url + ")", md)
        # 严格载荷不受渲染修复影响（url/final_url 语义原样）
        items = {i["url"]: i for i in cat["items"]}
        self.assertEqual(items[article_url]["final_url"], article_url)

    def test_background_citations_visible_unknown_retrospective_httpfailure(
            self):
        # report-citations P2 反例（背景）：无日期未确证 / 历史回顾 / 403
        # 失败背景连原始 URL 与来源域名都不渲染——修复后背景表显示原始
        # 引用链接 + 实际 source_domain，限制说明/日期语义不变
        from src.services import sector_research as _sr

        undated_url = "https://news.example.com/undated.html"
        retro_url = "https://news.example.com/retro.html"
        fail_url = "https://news.example.com/gone.html"
        today = datetime.now().date().isoformat()
        self._install_research_success(results=[
            {"title": "白酒行业动态无日期", "snippet": "白酒行业待确证",
             "url": undated_url, "source": "news.example.com",
             "published_date": None},
            {"title": "2026年1月白酒政策回顾", "snippet": "回顾旧政",
             "url": retro_url, "source": "news.example.com",
             "published_date": today},
            {"title": "白酒行业旧闻待探查", "snippet": "无日期条目",
             "url": fail_url, "source": "news.example.com",
             "published_date": None},
        ])
        self._install_terminal_probe({
            undated_url: (
                _sr.extract_article_date_evidence(
                    "<html><head><title>白酒行业动态无日期</title></head>"
                    "<body><p>正文</p></body></html>", undated_url),
                None, undated_url),
            fail_url: (_sr.ArticleDateEvidence(None, None, None),
                       "article_http_error:403", None),
        })
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        cat = body["analysis"]["sector_catalysts"]
        self.assertEqual(cat["items"], [])
        self.assertEqual(len(cat["background_items"]), 3)
        md = self._citations_readback(body)
        # 三类背景的原始引用链接 + 来源域名都可见（标题含数字时渲染
        # 管线做中西文间距规范化——链接目标断言不绑定标题原文）
        self.assertIn("[白酒行业动态无日期](" + undated_url + ")", md)
        self.assertIn("](https://news.example.com/retro.html)", md)
        self.assertIn("白酒政策回顾", md)
        self.assertIn("[白酒行业旧闻待探查](" + fail_url + ")", md)
        self.assertIn("news.example.com", md)
        # 限制语义保留（降级原因可见）
        self.assertIn("无可用发布日期证据", md)
        self.assertIn("历史回顾", md)
        self.assertIn("article_http_error", md)

    def test_redirect_report_shows_original_and_terminal_citations(self):
        # redirect 冲突背景：原始引用（表内链接）与实际终态地址行**同时**
        # 可见——终态行保留既有 provenance 语义，原引用不因终态存在而消失
        original = "https://news.example.com/original.html"
        terminal = "https://publisher.example.net/2020/11/06/final.html"
        ev = self._meta_evidence(terminal, datetime.now().date())
        self._install_research_success(results=[{
            "title": "白酒行业新政策发布", "snippet": "新规落地",
            "url": original, "source": "news.example.com",
            "published_date": None,
        }])
        self._install_terminal_probe({original: (ev, None, terminal)})
        self._install_vendors()
        resp = self.client.post(self._GENERATE)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        md = self._citations_readback(body)
        self.assertIn("[白酒行业新政策发布](" + original + ")", md)
        self.assertIn(terminal, md)  # 终态 provenance 行保留
        self.assertIn("publisher.example.net", md)  # 实际来源域名可见


class TestCompletenessJudgment(SectorReportApiV2TestCase):
    """完整性依据有效动态事实，独立于评分政策：

    - 行存在 ≠ 动态字段已取得（confirmed 但行情字段全缺 → partial + 原因）
    - 成分 unverified（归属未确认）→ partial
    - confirmed + 行情命中 + 核心动态有效 → complete
    - 部分可选维度缺口不机械降级（正常东财表无成交额列时如实记缺口）
    """

    def test_row_present_but_all_dynamic_missing_is_partial(self):
        """确认身份 + 行存在但涨跌幅/换手/成交额/广度全缺 → partial，不伪 complete。"""
        table = pd.DataFrame({"板块名称": ["白酒Ⅱ"], "板块代码": ["BK1277"]})
        self._mock_vendors(industry="白酒Ⅱ", boards=None, cons=["600519"],
                           board_table=table)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        a = resp.json()["analysis"]
        self.assertEqual(a["identity"]["subject_status"], "confirmed")
        intraday = a["background"]["intraday"]
        self.assertTrue(intraday["sector_found"])
        self.assertIsNone(intraday["momentum"])
        self.assertEqual(a["assessment"]["status"], "unscored")  # 评分政策不变
        self.assertEqual(a["assessment"]["completeness"], "partial")
        self.assertTrue(
            any("动态字段未取得" in r for r in a["assessment"]["reasons"]),
            a["assessment"]["reasons"],
        )

    def test_unverified_membership_is_partial(self):
        self._mock_vendors(industry="白酒Ⅱ",
                           boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
                           cons=["000568", "000596"])
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        assessment = resp.json()["analysis"]["assessment"]
        self.assertEqual(assessment["completeness"], "partial")
        self.assertTrue(
            any("归属未确认" in r for r in assessment["reasons"]),
            assessment["reasons"],
        )

    def test_partial_optional_dim_keeps_complete(self):
        """正常东财表无成交额列：成交额活跃度缺口如实，不降整份报告完整性。

        issue #82：趋势/同业必需块正常取得（合格窗口 mock），场景聚焦行情
        可选维度缺口——趋势未取得才是新的 partial 触发条件。
        """
        self._install_research_success()
        table = pd.DataFrame({
            "板块名称": ["白酒Ⅱ", "电池"], "板块代码": ["BK1277", "BK1033"],
            "涨跌幅": [1.65, 2.0], "换手率": [2.3, 3.0],
            "上涨家数": [18, 20], "下跌家数": [2, 5],
            "领涨股票": ["样本股", "电池股"], "领涨股票-涨跌幅": [5.5, 4.0],
        })
        expected = recent_completed_trading_sessions("cn", 20)
        self._mock_vendors(
            industry="白酒Ⅱ", boards=None, cons=["600519"], board_table=table,
            board_hist=_daily_hist_df(sorted(expected)),
            bench_hist=_daily_hist_df(sorted(expected), date_col="date",
                                      close_col="close"),
        )
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        a = resp.json()["analysis"]
        intraday = a["background"]["intraday"]
        self.assertIsNone(intraday["amount_activity"])
        self.assertIsNotNone(intraday["momentum"])
        self.assertTrue(any("成交额" in g for g in intraday["gaps"]))
        self.assertEqual(a["assessment"]["completeness"], "complete")


class TestFetchBudgetCoverage(SectorReportApiV2TestCase):
    """取数预算覆盖整条真实调用链（issue #81 第二轮反例）：

    - 无 stock_name 不再为补名打开无截止共享 manager（反例实测第二源在
      取数截止后 +0.10s 才启动新出站）
    - 合法 1s 小预算不被固定 5s 收尾余量立即耗尽（收尾余量 min(5, budget/2)）
    - 预算内取数被钳制：等待超时放弃、晚到 IO 不采纳、不存档伪成功
    """

    def test_missing_name_uses_code_not_shared_manager(self):
        """无 stock_name：名称如实用代码，不出站共享 manager 补名。"""
        import src.agent.tools.data_tools as data_tools

        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")],
                           cons=["600519"])
        with patch.object(data_tools, "_get_fetcher_manager") as manager_factory:
            resp = self.client.post(
                "/api/v1/sector-analysis/generate?stock_code=600519"
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["analysis"]["stock_name"], "600519")
        manager_factory.assert_not_called()

    def test_one_second_legal_budget_completes_report(self):
        """1s 是合法配置：收尾余量随预算缩放，取数不被固定 margin 立即耗尽。"""
        os.environ["SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS"] = "1"
        Config.reset_instance()
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")],
                           cons=["600519"])
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 1)

    def test_timeout_no_archive_and_late_io_not_adopted(self):
        """预算 1s（收尾余量 0.5s → 取数预算 0.5s）：供应商延迟 0.8s 超钳制
        → 放弃等待 → 504 不存档；晚到 IO 不采纳。"""
        os.environ["SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS"] = "1"
        Config.reset_instance()
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")],
                           cons=["600519"], info_delay=0.8)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 504, resp.text)
        self.assertIn("超时", resp.json()["message"])
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 0)  # 无伪成功记录
        self.assertFalse(self.report_dir.exists() and any(self.report_dir.iterdir()))


class TestPersistenceDeadline(SectorReportApiV2TestCase):
    """写文件与真实 DB 提交同一截止（issue #81 第二轮真实反例）：

    - 写入迟到（真实 write_text 注入延迟）：写后检查主截止、清理本请求独占
      文件，不开始 DB 保存，不留伪成功
    - 真实 SQLite 写锁（独立连接 BEGIN IMMEDIATE）：板块保存入口按同一
      monotonic deadline 钳制锁等待，提交前到期 rollback，不后台晚提交、
      不 commit 后伪报 timeout
    - deadline=None 默认调用语义逐字节不变（其他调用者回归）
    """

    def test_late_file_write_no_archive_no_db_save(self):
        """真实写入迟到超主截止：504、无记录、无残留文件。"""
        import time as _time

        import src.services.sector_analysis_service as svc

        os.environ["SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS"] = "2"
        Config.reset_instance()
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")],
                           cons=["600519"])
        original_write = Path.write_text

        def _delayed_write(path, data, *args, **kwargs):  # noqa: ANN001
            if Path(path).parent == self.report_dir:
                _time.sleep(2.2)  # 写入本身迟到：渲染后检查已过，写后须复核
            return original_write(path, data, *args, **kwargs)

        with patch.object(Path, "write_text", _delayed_write):
            resp = self.client.post(
                "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
            )
        self.assertEqual(resp.status_code, 504, resp.text)
        self.assertIn("超时", resp.json()["message"])
        self.assertEqual(self.client.get("/api/v1/sector-analysis/reports").json()["total"], 0)
        self.assertFalse(self.report_dir.exists() and any(self.report_dir.iterdir()))

    def test_real_sqlite_write_lock_clamped_to_deadline(self):
        """独立连接持真实 SQLite 写锁：保存锁等待钳制剩余预算，到期不提交。

        锁由本测试主线程跨**整个请求**持有（BEGIN IMMEDIATE 后无定时释放
        线程——CI 排队/启动延迟下 5s 定时释放会在请求开始前回滚锁，使保存
        在 3s 预算内合法成功 200）；请求结束后 finally 内 rollback/close。
        """
        import sqlite3
        import time as _time

        import src.services.sector_analysis_service as svc

        os.environ["SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS"] = "3"
        Config.reset_instance()
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")],
                           cons=["600519"])
        locker = sqlite3.connect(self.db_path)
        locker.execute("BEGIN IMMEDIATE")  # 真实写锁：保存事务将等待
        original_render = svc._render

        def _slow_render(*args, **kwargs):  # noqa: ANN001
            md = original_render(*args, **kwargs)
            _time.sleep(1.2)  # 保留真实渲染器，随后进入有界保存（剩余 < 2s）
            return md

        try:
            with patch.object(svc, "_render", _slow_render):
                resp = self.client.post(
                    "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
                )
        finally:
            locker.rollback()
            locker.close()
        self.assertEqual(resp.status_code, 504, resp.text)
        self.assertEqual(self.client.get("/api/v1/sector-analysis/reports").json()["total"], 0)
        self.assertFalse(self.report_dir.exists() and any(self.report_dir.iterdir()))

    def test_save_deadline_future_and_default_semantics_unchanged(self):
        """未来 deadline 正常提交；无 deadline 默认调用（其他调用者）语义不变。"""
        import time as _time

        base_record = {
            "id": "sa_202611050901", "stock_code": "600519", "stock_name": "贵州茅台",
            "md_path": str(self.report_dir / "sa_202611050901.md"),
            "sector_score": None, "analysis_json": "{}",
        }
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.assertTrue(
            self.db.save_sector_analysis_report(
                dict(base_record), deadline=_time.monotonic() + 5)
        )
        self.assertIsNotNone(self.db.get_sector_analysis_report("sa_202611050901"))
        legacy = dict(base_record, id="sa_202611050902",
                      md_path=str(self.report_dir / "sa_202611050902.md"))
        self.assertTrue(self.db.save_sector_analysis_report(legacy))  # 默认无 deadline
        self.assertIsNotNone(self.db.get_sector_analysis_report("sa_202611050902"))


class TestDeleteFeedback(SectorReportApiV2TestCase):
    """历史删除完整反馈（issue #81 第二轮反例）：DB 已删但文件清理失败时
    不静默 200 假成功，响应如实区分「记录已删除、文件清理失败」；重复删除 404。
    """

    def test_delete_file_cleanup_failure_is_reported(self):
        """真实 OSError 注入：存档路径替换为同名目录 → unlink 失败。"""
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")],
                           cons=["600519"])
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        rid = resp.json()["report_id"]
        (self.report_dir / f"{rid}.md").unlink()
        (self.report_dir / f"{rid}.md").mkdir()  # 同名目录使 unlink 抛 IsADirectoryError
        result = self.client.delete(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertTrue(body["success"])  # 记录确实已删
        self.assertEqual(body["file_cleanup"], "failed")
        self.assertIn("清理失败", body["message"])
        # 记录已删除：详情/下载/再次删除均 404，无误导
        self.assertEqual(
            self.client.get(f"/api/v1/sector-analysis/reports/{rid}").status_code, 404)
        self.assertEqual(
            self.client.delete(f"/api/v1/sector-analysis/reports/{rid}").status_code, 404)
        # 注入目录清理（临时资源完整性）
        (self.report_dir / f"{rid}.md").rmdir()

    def test_delete_success_reports_cleanup_ok(self):
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")],
                           cons=["600519"])
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        rid = resp.json()["report_id"]
        result = self.client.delete(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["file_cleanup"], "ok")


class TestEnvIsolation(SectorReportApiV2TestCase):
    """测试隔离基座回归（第二轮审查 sentinel 反例）：临时 env 从**修改前**
    备份恢复，继承的 process env 不被 tearDown 删除；仅合成 sentinel，
    不读真实凭据；Config/DB 单例由基座 tearDown reset。"""

    def test_concept_case_restores_inherited_env(self):
        sentinel_endpoint = "https://ifind-sentinel.example.invalid/endpoint"
        sentinel_token = "sentinel-token-not-a-real-secret"
        backup_endpoint = os.environ.get("IFIND_MCP_ENDPOINT")
        backup_token = os.environ.get("IFIND_MCP_TOKEN")
        os.environ["IFIND_MCP_ENDPOINT"] = sentinel_endpoint
        os.environ["IFIND_MCP_TOKEN"] = sentinel_token
        case = TestGenerateClosedLoop(
            methodName="test_concepts_over_display_cap_partial_report_saved")
        try:
            case.setUp()
            try:
                case.test_concepts_over_display_cap_partial_report_saved()
            finally:
                case.tearDown()
                case.doCleanups()
            # 内层 tearDown 必须恢复到修改前值（sentinel），不误删继承值
            self.assertEqual(os.environ.get("IFIND_MCP_ENDPOINT"), sentinel_endpoint)
            self.assertEqual(os.environ.get("IFIND_MCP_TOKEN"), sentinel_token)
        finally:
            # 外层自身清理：恢复本测试修改前的进程环境
            if backup_endpoint is None:
                os.environ.pop("IFIND_MCP_ENDPOINT", None)
            else:
                os.environ["IFIND_MCP_ENDPOINT"] = backup_endpoint
            if backup_token is None:
                os.environ.pop("IFIND_MCP_TOKEN", None)
            else:
                os.environ["IFIND_MCP_TOKEN"] = backup_token


class TestLegacyRecordCompat(SectorReportApiV2TestCase):
    """旧记录缺新字段：仍可回看/下载；列表可见旧综合分。"""

    def test_legacy_record_readable_and_downloadable(self):
        legacy_analysis = {
            "stock_code": "600519", "stock_name": "贵州茅台",
            "identification": {"industry_em": "白酒", "sector_name": "白酒",
                               "sw_chain": {}, "gaps": []},
            "policy": {"lean": "neutral", "label": "中性", "score": 50.0, "gaps": []},
            "base_rate": {"base_rate": 0.55, "basis": "industry_base_rate:白酒",
                          "hit_keyword": "白酒", "is_default": False,
                          "table_version": "v1.0", "table_updated": "2026-09-29",
                          "table_industry_count": 21, "percentile": 0.95, "all_rates": []},
            "prosperity": {"sector_found": True, "momentum": 70.0, "activity": 60.0,
                           "capital": 50.0, "breadth": 90.0, "rank_of": 10,
                           "total_boards": 86, "row": {}, "gaps": []},
            "prosperity_score": 67.5, "sector_score": 61.5, "band": "中性",
            "beta_meaning": "板块β基本中性", "weights": {"policy": 0.3, "base_rate": 0.3,
                                                        "prosperity": 0.4},
            "gaps": [],
        }
        md_path = self.report_dir / "sa_202611010900.md"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# 贵州茅台（600519）板块分析报告\n\n- 旧版综合板块得分：61.5/100 —— 中性\n",
                           encoding="utf-8")
        self.assertTrue(self.db.save_sector_analysis_report({
            "id": "sa_202611010900", "stock_code": "600519", "stock_name": "贵州茅台",
            "md_path": str(md_path), "sector_score": 61.5,
            "analysis_json": json.dumps(legacy_analysis, ensure_ascii=False),
        }))
        detail = self.client.get("/api/v1/sector-analysis/reports/sa_202611010900")
        self.assertEqual(detail.status_code, 200)
        d = detail.json()["data"]
        self.assertEqual(d["sector_score"], 61.5)
        self.assertEqual(d["analysis_json"]["band"], "中性")  # 旧字段原样可读
        self.assertIn("61.5", d["markdown"])
        download = self.client.get("/api/v1/sector-analysis/reports/sa_202611010900/markdown")
        self.assertEqual(download.status_code, 200)
        self.assertIn("旧版综合板块得分", download.text)

        # 新旧共存：新报告生成后列表两者都可见（新记录评分字段显式 None）
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")], cons=["600519"])
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        lst = self.client.get("/api/v1/sector-analysis/reports").json()
        self.assertEqual(lst["total"], 2)
        scores = {item["id"]: item["sector_score"] for item in lst["data"]}
        self.assertEqual(scores["sa_202611010900"], 61.5)
        self.assertIsNone(scores[resp.json()["report_id"]])

    def test_delete_then_404(self):
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")], cons=["600519"])
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        rid = resp.json()["report_id"]
        self.assertEqual(self.client.delete(f"/api/v1/sector-analysis/reports/{rid}").status_code, 200)
        self.assertEqual(
            self.client.delete(f"/api/v1/sector-analysis/reports/{rid}").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/v1/sector-analysis/reports/{rid}").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/v1/sector-analysis/reports/{rid}/markdown").status_code, 404
        )


class TestStorageDeadlineConnectionLifecycle(SectorReportApiV2TestCase):
    """契约 A（第三轮）：deadline 保存不得把连接级 busy_timeout 泄漏进共享池。

    真实 SQLite + 连续调用 + 独立连接真实写锁：一次短预算 deadline 保存后，
    归还池的物理连接必须恢复引擎默认 busy_timeout；后续无 deadline 普通写入
    在 200ms 锁竞争下必须等到锁释放而不是被残留的短钳制提前打断。
    """

    def test_deadline_save_restores_pooled_busy_timeout_for_later_writes(self):
        import sqlite3
        import time as _time

        original_ms = self._pooled_busy_timeout_ms()
        record = {"id": "sa_lifecycle_a1", "stock_code": "600519",
                  "stock_name": "synthetic", "md_path": "/temporary/synthetic.md",
                  "analysis_json": "{}", "sector_score": None}
        # 清池：下一条物理连接正是 deadline 保存所用并归还的那条（可观察恢复）
        self.db._engine.dispose()
        self.assertTrue(self.db.save_sector_analysis_report(
            record, deadline=_time.monotonic() + 0.05))
        self.assertEqual(self._pooled_busy_timeout_ms(), original_ms)

        # 连续真实写：独立连接持锁 200ms，无 deadline 写入等到锁释放后成功
        lock = sqlite3.connect(str(self.db_path), check_same_thread=False)
        lock.execute("BEGIN IMMEDIATE")

        def _release():
            _time.sleep(0.2)
            lock.rollback()

        worker = threading.Thread(target=_release)
        worker.start()
        started = _time.monotonic()
        second = self.db.save_sector_analysis_report({**record, "id": "sa_lifecycle_a2"})
        elapsed = _time.monotonic() - started
        worker.join()
        lock.close()
        self.assertTrue(second)
        self.assertGreaterEqual(elapsed, 0.19)

    def test_repeated_deadline_saves_all_restore_pooled_pragma(self):
        """连续多次 deadline 保存（成功路径反复归还）后池连接仍是原值。"""
        import time as _time

        original_ms = self._pooled_busy_timeout_ms()
        self.db._engine.dispose()
        for i in range(3):
            self.assertTrue(self.db.save_sector_analysis_report(
                {"id": f"sa_lifecycle_r{i}", "stock_code": "600519",
                 "stock_name": "synthetic", "md_path": "/temporary/x.md",
                 "analysis_json": "{}", "sector_score": None},
                deadline=_time.monotonic() + 0.2))
        self.assertEqual(self._pooled_busy_timeout_ms(), original_ms)


class TestFlushDeadlineBoundary(SectorReportApiV2TestCase):
    """契约 B（第三轮）：真实 INSERT/flush 之后仍须复核同一截止。

    保留真实 ORM flush/INSERT/COMMIT 与真实 SQLite 文件；仅在 after_flush
    事件注入受控调度/慢 flush 延迟（不替换 commit、不 mock 数据库）。
    """

    def _slow_flush_patcher(self, seconds: float):
        import time as _time

        from sqlalchemy import event

        def slow_flush(session, context):  # noqa: ANN001 - SQLAlchemy 事件签名
            _time.sleep(seconds)

        event.listen(self.db._SessionLocal, "after_flush", slow_flush)
        self.addCleanup(event.remove, self.db._SessionLocal, "after_flush", slow_flush)

    def test_slow_flush_after_insert_rolls_back_not_commits(self):
        import time as _time

        record = {"id": "sa_flush_b1", "stock_code": "600519",
                  "stock_name": "synthetic", "md_path": "/temporary/x.md",
                  "analysis_json": "{}", "sector_score": None}
        original_ms = self._pooled_busy_timeout_ms()
        self._slow_flush_patcher(0.25)
        started = _time.monotonic()
        saved = self.db.save_sector_analysis_report(record, deadline=started + 0.1)
        elapsed = _time.monotonic() - started
        self.assertFalse(saved)  # flush 后预算已尽：rollback 不提交
        self.assertGreaterEqual(elapsed, 0.25)  # 真实 flush 延迟被完整经历
        self.assertIsNone(self.db.get_sector_analysis_report("sa_flush_b1"))
        # 失败路径同样归还干净连接（A/B 同一生命周期：恢复在归还池之前）
        self.assertEqual(self._pooled_busy_timeout_ms(), original_ms)

    def test_api_slow_flush_over_budget_no_archive(self):
        """API 反例（第三轮）：合法 1s 预算 + flush 1.2s → 504、0 新行、0 文件。"""
        os.environ["SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS"] = "1.0"
        Config.reset_instance()
        self._mock_vendors(industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")], cons=["600519"])
        self._slow_flush_patcher(1.2)
        before_total = self.client.get("/api/v1/sector-analysis/reports").json()["total"]
        response = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=synthetic")
        after_total = self.client.get("/api/v1/sector-analysis/reports").json()["total"]
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(after_total - before_total, 0)
        self.assertFalse(self.report_dir.exists() and any(self.report_dir.iterdir()))


class TestSupplierNonFiniteNumericQualification(SectorReportApiV2TestCase):
    """契约 D（第三轮）：供应商 NaN 不是有效行情——缺失走 partial+具体缺口，
    不伪造动量分位/正文 nan%；其余有效事实与存档语义保持正常。"""

    def test_nan_change_pct_partial_not_pseudo_complete(self):
        table = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033"), ("银行", "BK0475")])
        table.loc[0, "涨跌幅"] = float("nan")
        self._mock_vendors(industry="白酒Ⅱ", boards=None, cons=["600519"],
                           board_table=table)
        response = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        analysis = body["analysis"]
        intraday = analysis["background"]["intraday"]
        # 行命中（其他事实保留）但涨跌幅缺失：无动量/排名，缺口如实
        self.assertTrue(intraday["sector_found"])
        self.assertEqual(intraday["total_boards"], 3)
        self.assertIsNone(intraday["row"]["change_pct"])
        self.assertIsNone(intraday["momentum"])
        self.assertIsNone(intraday["rank_of"])
        self.assertEqual(intraday["row"]["turnover_rate"], 2.3)
        self.assertEqual(intraday["breadth"], 90.0)
        self.assertTrue(intraday["gaps"])  # 具体缺口（动量）必须透出
        self.assertTrue(any("动量" in g for g in intraday["gaps"]))
        assessment = analysis["assessment"]
        self.assertEqual(assessment["completeness"], "partial")
        self.assertTrue(assessment["reasons"])
        self.assertTrue(any("动量" in r for r in assessment["reasons"]))
        # API 响应与存档一致：change_pct null、无 nan 正文
        self.assertIn("change_pct", response.text)
        self.assertIsNone(intraday["row"]["change_pct"])
        # 真实临时 SQLite + 文件：存档语义正常（不是 500/不存档）
        rows, total = self.db.list_sector_analysis_reports()
        self.assertEqual(total, 1)
        self.assertEqual(len(list(self.report_dir.glob("*.md"))), 1)
        rid = rows[0]["id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        download = self.client.get(f"/api/v1/sector-analysis/reports/{rid}/markdown")
        self.assertEqual(download.status_code, 200)
        self.assertNotIn("nan%", download.text)
        self.assertNotIn("nan%", body["markdown"])

    def test_inf_change_pct_also_missing(self):
        """无穷大同理：非有限一律缺失，不参与分位、不写正文。"""
        table = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        table.loc[0, "涨跌幅"] = float("inf")
        self._mock_vendors(industry="白酒Ⅱ", boards=None, cons=["600519"],
                           board_table=table)
        response = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台")
        self.assertEqual(response.status_code, 200, response.text)
        intraday = response.json()["analysis"]["background"]["intraday"]
        self.assertIsNone(intraday["row"]["change_pct"])
        self.assertIsNone(intraday["momentum"])
        self.assertIsNone(intraday["rank_of"])
        self.assertEqual(response.json()["analysis"]["assessment"]["completeness"],
                         "partial")

    def test_peer_nonfinite_suppresses_rank_full_loop(self):
        """端端反例（第四轮 spec FAIL1）：同行非有限不产生假全表排名。

        真实 API 反例复现：四板块涨幅 [1.65, +inf, -0.5, NaN]，目标 1.65
        是有效最高却曾输出 momentum=100 / rank 3/4 / gaps=[] / complete
        （旧 rank=total-below 把有效集合统计映射到观测全表名次），并经
        真实临时 SQLite/存档文件/详情/下载一致复制。修正后：动量按有效
        样本（100），无全表名次（None + 缺口注明有效样本数/观测总量），
        completeness=complete——动量有效，同行缺失只是排名资格缺口，
        不机械降级整份报告。
        """
        self._install_research_success()
        table = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033"),
                              ("银行", "BK0475"), ("地产", "BK0476")])
        table["涨跌幅"] = [1.65, float("inf"), -0.5, float("nan")]
        expected = recent_completed_trading_sessions("cn", 20)
        self._mock_vendors(
            industry="白酒Ⅱ", boards=None, cons=["600519"], board_table=table,
            board_hist=_daily_hist_df(sorted(expected)),
            bench_hist=_daily_hist_df(sorted(expected), date_col="date",
                                      close_col="close"),
        )
        response = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        analysis = body["analysis"]
        intraday = analysis["background"]["intraday"]
        self.assertTrue(intraday["sector_found"])
        self.assertEqual(intraday["row"]["change_pct"], 1.65)
        self.assertEqual(intraday["momentum"], 100.0)
        self.assertIsNone(intraday["rank_of"])
        self.assertEqual(intraday["total_boards"], 4)
        rank_gaps = [g for g in intraday["gaps"] if "全表排名" in g]
        self.assertEqual(len(rank_gaps), 1)
        self.assertIn("观测 4", rank_gaps[0])
        self.assertIn("有效样本仅 2", rank_gaps[0])
        # 同行缺失只是排名资格缺口：动量有效 → 不机械降级为 partial
        self.assertEqual(analysis["assessment"]["status"], "unscored")
        self.assertEqual(analysis["assessment"]["completeness"], "complete")
        # 真实临时 SQLite + 存档文件：修正后语义持久化一致
        _, total = self.db.list_sector_analysis_reports()
        self.assertEqual(total, 1)
        self.assertEqual(len(list(self.report_dir.glob("*.md"))), 1)
        rid = body["report_id"]
        record = self.db.get_sector_analysis_report(rid)
        self.assertIsNotNone(record)
        persisted = json.loads(record["analysis_json"])
        self.assertIsNone(persisted["background"]["intraday"]["rank_of"])
        self.assertEqual(persisted["background"]["intraday"]["gaps"], intraday["gaps"])
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        download = self.client.get(f"/api/v1/sector-analysis/reports/{rid}/markdown")
        self.assertEqual(download.status_code, 200)
        # 正文不再有假全表名次：排名行显式 N/A，缺口章节解释不提供的原因
        self.assertNotIn("全表排名：3 / 4", download.text)
        self.assertIn("全表排名：N/A / 4", download.text)
        self.assertTrue(any(
            "全表排名" in line and "不提供" in line
            for line in download.text.splitlines() if line.startswith("- ")))
        # 生成-详情-下载正文一致
        self.assertEqual(download.text, body["markdown"])
        self.assertEqual(detail.json()["data"]["markdown"], body["markdown"])


class TestTrendPeersClosedLoop(SectorReportApiV2TestCase):
    """issue #82 反例矩阵（公开 API 主缝）：供应商边界隔离，比较/选样/渲染/持久化真实。

    覆盖：目标在前五/不在前五与生成-详情-下载一致、unverified 不出站、
    严格日历不可用不出站、盘中窗口含供应商多给当日行被剔除、行业缺 1 天
    旧日凑数、基准日期不同、双方缺同日、乱序、重复日期、NaN/inf/0/负收盘、
    同业金额缺失/0/负/非有限与重复代码与不足 5、板块合格+基准供应商失败
    各自保留。
    """

    def _real_window(self) -> list:
        sessions = recent_completed_trading_sessions("cn", 20)
        self.assertIsNotNone(sessions)
        return sorted(sessions)

    def _generate_confirmed(self, *, board_hist, bench_hist, cons_codes=None,
                            cons_df=None, calendar_sessions="real"):
        # issue #83：confirmed 闭环场景安装合成搜索/模型终端（终端响应隔离；
        # 准入/登记表/校验/渲染/存档真实）——82 的 complete 语义在 83 下
        # 依赖催化+综合成功取得，二者在本类场景统一注入。
        self._install_research_success()
        # 板块全表给 2 行（momentum 比较样本 ≥2，行情支柱完整；本类聚焦趋势/同业）
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

    # ---- 正常路径变体 ----

    def test_target_not_in_top5_sample_six_and_download_consistent(self):
        expected = self._real_window()
        board_hist = _daily_hist_df(expected, base=1000.0, step=1.0)
        bench_hist = _daily_hist_df(expected, base=4000.0, step=1.0,
                                    date_col="date", close_col="close")
        cons = _cons_df(["600001", "600002", "600003", "600004", "600005", "600519"])
        resp = self._generate_confirmed(board_hist=board_hist, bench_hist=bench_hist,
                                        cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        trend, peers = analysis["sector_trend"], analysis["sector_peers"]
        self.assertTrue(trend["eligible"] and peers["eligible"])
        self.assertIsNotNone(trend["relative_return_pp"])
        self.assertFalse(peers["target_in_amount_top5"])
        self.assertEqual(peers["sample_size"], 6)
        codes = [i["code"] for i in peers["items"]]
        self.assertEqual(codes[:5], ["600001", "600002", "600003", "600004", "600005"])
        self.assertEqual(codes[5], "600519")
        self.assertTrue(peers["items"][5]["is_target"])
        # 目标位置：快照涨跌幅降序（600519 涨跌幅 1.0 + 5*0.1 = 1.5 最高 → 第 1）
        self.assertEqual(peers["target_change_pct_rank"], 1)
        # 生成 → 详情 → 下载一致（趋势/同业值与缺口随载荷一致，正文为存档原文）
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        detail_analysis = detail.json()["data"]["analysis_json"]
        self.assertEqual(detail_analysis["sector_trend"]["period_return_pct"],
                         trend["period_return_pct"])
        self.assertEqual(detail_analysis["sector_peers"]["items"], peers["items"])
        download = self.client.get(f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.text, resp.json()["markdown"])
        self.assertIn("区间收益差", download.text)
        self.assertIn("成交活跃代表，非市值/基本面龙头", download.text)
        self.assertIn("个百分点", download.text)

    def test_target_in_top5_sample_five(self):
        expected = self._real_window()
        cons = _cons_df(["600519", "600001", "600002", "600003", "600004"])
        # _cons_df 成交额按序递减 → 600519 是成交额第一（在前五）
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected), bench_hist=_daily_hist_df(
                expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        self.assertTrue(peers["target_in_amount_top5"])
        self.assertEqual(peers["sample_size"], 5)
        codes = [i["code"] for i in peers["items"]]
        self.assertEqual(len(set(codes)), 5)
        self.assertNotIn("600519 双重计入", str(codes))

    # ---- 资格边界 ----

    def test_unverified_membership_no_trend_outbound(self):
        """成分验证未通过：不发趋势/基准出站，趋势/同业块 eligible=False。"""
        expected = self._real_window()
        boards = _board_table([("白酒Ⅱ", "BK1277")])
        # 成分不含目标股 → unverified
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277")],
            cons=["600001", "600002"], board_table=boards,
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
        )
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["identity"]["subject_status"], "unverified")
        self.assertFalse(analysis["sector_trend"]["eligible"])
        self.assertFalse(analysis["sector_peers"]["eligible"])
        self.assertEqual(self.hist_calls, [])
        self.assertEqual(self.bench_calls, [])
        self.assertIn("不执行板块趋势查询", analysis["sector_trend"]["gaps"][0])
        self.assertIn("不执行同业选取", analysis["sector_peers"]["gaps"][0])

    def test_calendar_unavailable_no_outbound_explicit_gap(self):
        """严格日历 None：明确缺口，趋势/基准不出站（不自然日 fallback）。"""
        resp = self._generate_confirmed(
            board_hist=None, bench_hist=None, calendar_sessions=None)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        trend = analysis["sector_trend"]
        self.assertTrue(trend["eligible"])
        self.assertIsNone(trend["period_return_pct"])
        self.assertEqual(self.hist_calls, [])
        self.assertEqual(self.bench_calls, [])
        self.assertTrue(any("交易日历" in g for g in trend["gaps"]))
        self.assertEqual(analysis["assessment"]["completeness"], "partial")
        self.assertTrue(any("板块近期趋势未取得" in r
                            for r in analysis["assessment"]["reasons"]))

    # ---- 窗口与数据资格 ----

    def test_intraday_window_drops_unclosed_session_row(self):
        """盘中窗口（显式 current_time，真实严格日历）+ 供应商多给未收盘
        当日行：当日 K 线在期望窗口外被剔除，20 个已完成观测保留。

        current_time 固定为盘中时刻（真实 ``recent_completed_trading_sessions``
        支持的 API；当日会话未收盘不计入已完成窗口），供应商硬编码的当日
        部分行永远落在期望窗口之外——与实际挂钟时间无关，不再产生窗口内
        重复日期。"""
        import datetime as _dt

        intraday_now = _dt.datetime(2026, 11, 6, 10, 0)
        sessions = recent_completed_trading_sessions(
            "cn", 20, current_time=intraday_now)
        self.assertIsNotNone(sessions)
        expected = sorted(sessions)
        self.assertNotIn(_dt.date(2026, 11, 6), expected)  # 盘中当日未完成
        board_hist = _daily_hist_df(expected, extra_rows=[
            {"日期": "2026-11-06", "收盘": 1099.0},  # 窗口外（未收盘）部分 K 线
        ])
        bench_hist = _daily_hist_df(expected, date_col="date", close_col="close")
        resp = self._generate_confirmed(board_hist=board_hist, bench_hist=bench_hist,
                                        calendar_sessions=sessions)
        self.assertEqual(resp.status_code, 200, resp.text)
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertIsNotNone(trend["period_return_pct"])
        self.assertEqual(trend["end_date"], expected[-1].isoformat())
        self.assertEqual(len(trend["observations"]), 20)  # 已完成观测保留
        self.assertNotIn("2026-11-06",
                         [o["date"] for o in trend["observations"]])
        self.assertEqual(trend["gaps"], [])

    def test_board_missing_day_extra_old_day_cannot_fill(self):
        """板块缺 1 天 + 供应商多给 1 个旧日：不凑 20；基准 20/20 各自保留。"""
        expected = self._real_window()
        import datetime as _dt
        old = _dt.date(expected[0].year - 1, expected[0].month, expected[0].day)
        board_hist = _daily_hist_df(
            expected[1:],  # 缺最早一天
            extra_rows=[{"日期": old.strftime("%Y-%m-%d"), "收盘": 99.0}],
        )
        bench_hist = _daily_hist_df(expected, date_col="date", close_col="close")
        resp = self._generate_confirmed(board_hist=board_hist, bench_hist=bench_hist)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        trend = analysis["sector_trend"]
        self.assertIsNone(trend["period_return_pct"])
        self.assertTrue(any("不取交集" in g and "板块日线" in g for g in trend["gaps"]))
        self.assertIsNotNone(trend["benchmark"]["period_return_pct"])
        self.assertIsNone(trend["relative_return_pp"])
        self.assertEqual(analysis["assessment"]["completeness"], "partial")

    def test_benchmark_date_set_differs_no_relative(self):
        """基准日期集与期望不一致：基准块不合格，板块保留，无相对。"""
        expected = self._real_window()
        import datetime as _dt
        bench_dates = expected[:-1] + [_dt.date(2026, 1, 2)]
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(sorted(bench_dates),
                                      date_col="date", close_col="close"))
        self.assertEqual(resp.status_code, 200, resp.text)
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertIsNotNone(trend["period_return_pct"])
        self.assertIsNone(trend["benchmark"]["period_return_pct"])
        self.assertIsNone(trend["relative_return_pp"])

    def test_both_missing_same_day_neither_return(self):
        """双方缺同一日：各 19/20 都无区间收益，更无相对（不凑 20）。"""
        expected = self._real_window()
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected[1:]),
            bench_hist=_daily_hist_df(expected[1:], date_col="date", close_col="close"))
        self.assertEqual(resp.status_code, 200, resp.text)
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertIsNone(trend["period_return_pct"])
        self.assertIsNone(trend["benchmark"]["period_return_pct"])
        self.assertIsNone(trend["relative_return_pp"])
        self.assertEqual(resp.json()["analysis"]["assessment"]["completeness"], "partial")

    def test_out_of_order_rows_still_qualify(self):
        expected = self._real_window()
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected).sample(frac=1.0, random_state=3),
            bench_hist=_daily_hist_df(expected, date_col="date",
                                      close_col="close").sample(frac=1.0, random_state=5))
        self.assertEqual(resp.status_code, 200, resp.text)
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertIsNotNone(trend["period_return_pct"])
        self.assertEqual(trend["gaps"], [])

    def test_duplicate_date_conflict_rejects_board_side(self):
        expected = self._real_window()
        board_hist = _daily_hist_df(expected, extra_rows=[
            {"日期": expected[5].strftime("%Y-%m-%d"), "收盘": 999.0},
        ])
        resp = self._generate_confirmed(
            board_hist=board_hist,
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"))
        self.assertEqual(resp.status_code, 200, resp.text)
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertIsNone(trend["period_return_pct"])
        self.assertTrue(any("重复日期" in g for g in trend["gaps"]))

    def test_non_finite_and_non_positive_closes_invalidate_days(self):
        expected = self._real_window()
        rows = {}
        for idx, val in ((3, float("nan")), (7, float("inf")), (11, 0.0), (15, -12.0)):
            rows[expected[idx]] = val
        board_hist = _daily_hist_df(expected)
        for d, v in rows.items():
            board_hist.loc[board_hist["日期"] == d.strftime("%Y-%m-%d"), "收盘"] = v
        resp = self._generate_confirmed(
            board_hist=board_hist,
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"))
        self.assertEqual(resp.status_code, 200, resp.text)
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertIsNone(trend["period_return_pct"])
        for d in (expected[3], expected[7], expected[11], expected[15]):
            self.assertTrue(any(d.isoformat() in g for g in trend["gaps"]),
                            f"缺口缺 {d}: {trend['gaps']}")

    def test_board_ok_bench_vendor_down_keeps_board_side(self):
        """板块合格 + 基准供应商失败：板块区间收益保留，相对不算，不整份失败。"""
        expected = self._real_window()
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected), bench_hist=None)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        trend = analysis["sector_trend"]
        self.assertIsNotNone(trend["period_return_pct"])
        self.assertIsNone(trend["benchmark"]["period_return_pct"])
        self.assertIsNone(trend["relative_return_pp"])
        self.assertTrue(trend["benchmark"]["gaps"])
        self.assertEqual(analysis["assessment"]["completeness"], "complete")
        self.assertTrue(any("基准" in g for g in trend["gaps"]))

    # ---- 同业选取反例 ----

    def test_peers_invalid_amount_rows_excluded_under5_gap(self):
        expected = self._real_window()
        cons = pd.DataFrame({
            "代码": ["600001", "600002", "600003", "600004", "600005", "600519"],
            "名称": [f"股票{c}" for c in [
                "600001", "600002", "600003", "600004", "600005", "600519"]],
            "涨跌幅": [1.0] * 6,
            "成交额": [9e8, None, 0.0, float("nan"), -5.0, 2e8],
        })
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        codes = [i["code"] for i in peers["items"]]
        self.assertEqual(codes, ["600001", "600519"])  # 仅 2 家有效
        self.assertEqual(peers["sample_size"], 2)
        self.assertTrue(any("不足 5 家" in g for g in peers["gaps"]))
        self.assertIsNotNone(peers["target_change_pct_rank"])

    def test_peers_duplicate_code_conflict_dropped(self):
        expected = self._real_window()
        cons = _cons_df(["600001", "600002", "600003", "600004", "600005", "600519"])
        dup = pd.DataFrame([{"代码": "600002", "名称": "股票600002乙",
                             "涨跌幅": 9.9, "成交额": 1e10}])
        cons = pd.concat([cons, dup], ignore_index=True)
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        codes = [i["code"] for i in peers["items"]]
        self.assertNotIn("600002", codes)
        self.assertTrue(any("重复代码" in g for g in peers["gaps"]))

    def test_peers_snapshot_without_amount_column_records_gap(self):
        """快照无成交额列：已确认目标股保留（代码/名称/涨跌幅），不产金额 top5、
        不发 1/1 伪名次；可选缺口不机械 insufficient（审查反例：旧版整丢目标）。"""
        expected = self._real_window()
        cons = pd.DataFrame({"代码": ["600519", "600001"], "名称": ["贵州茅台", "股票600001"],
                             "涨跌幅": [1.65, 1.0]})
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        peers = analysis["sector_peers"]
        self.assertTrue(peers["eligible"])
        self.assertEqual(peers["sample_size"], 1)
        self.assertEqual(peers["rankable_rows"], 0)
        targets = [i for i in peers["items"] if i["is_target"]]
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["code"], "600519")
        self.assertIsNone(targets[0]["amount"])
        self.assertEqual(targets[0]["change_pct"], 1.65)
        self.assertIsNone(peers["target_in_amount_top5"])
        self.assertIsNone(peers["target_change_pct_rank"])  # 无可比同行不伪名次
        self.assertTrue(any("无成交额列" in g for g in peers["gaps"]))
        # 可选正常缺口不机械不足：行情/趋势核心取得 → complete，缺口由块内如实透出
        self.assertEqual(analysis["assessment"]["completeness"], "complete")
        self.assertFalse(any("同业样本未取得" in r
                             for r in analysis["assessment"]["reasons"]))
        md, _, _ = self._full_detail_download(resp.json())
        self.assertIn("| 600519 | 贵州茅台 | N/A | 1.65% | ✔ |", md)
        self.assertIn("无成交额列", md)
        self.assertIn("无可比", md)

    def test_peers_target_missing_change_pct_rank_none(self):
        expected = self._real_window()
        cons = _cons_df(["600519", "600001"])
        cons.loc[cons["代码"] == "600519", "涨跌幅"] = float("nan")
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        target = [i for i in peers["items"] if i["is_target"]][0]
        self.assertIsNone(target["change_pct"])
        self.assertIsNone(peers["target_change_pct_rank"])
        self.assertTrue(any("相对位置不输出" in g or "涨跌幅缺失" in g
                            for g in peers["gaps"]))

    # ---- issue #82 rework：审查反例收敛回归（生成→JSON→Markdown→详情→下载一致）----

    def _full_detail_download(self, resp: dict) -> tuple[str, str, str]:
        rid = resp["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        dl = self.client.get(f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(dl.status_code, 200)
        md = resp["markdown"]
        self.assertEqual(detail.json()["data"]["markdown"], md)
        self.assertEqual(dl.text, md)
        self.assertEqual(detail.json()["data"]["analysis_json"]["sector_trend"],
                         resp["analysis"]["sector_trend"])
        self.assertEqual(detail.json()["data"]["analysis_json"]["sector_peers"],
                         resp["analysis"]["sector_peers"])
        return md, detail.json()["data"]["analysis_json"], dl.text

    def test_board_partial19_keeps_observations_and_benchmark_in_markdown(self):
        """板块 19/20 + 基准 20/20：板块部分观测保留，合格基准收益与观测进正文。

        反例（spec P4）：基准展示曾嵌在板块收益成功分支，板块残缺时正文丢失
        已合格的基准事实；现在板块与基准独立渲染。
        """
        expected = self._real_window()
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected).iloc[1:],
            bench_hist=_daily_hist_df(expected, base=4000.0, step=1.0,
                                      date_col="date", close_col="close"),
            cons_df=_cons_df(["600519", "000568"]))
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        trend = analysis["sector_trend"]
        self.assertIsNone(trend["period_return_pct"])
        self.assertEqual(trend["qualified_sessions"], 19)
        self.assertEqual(len(trend["observations"]), 19)  # 部分合格观测保留
        self.assertIsNotNone(trend["benchmark"]["period_return_pct"])
        self.assertEqual(len(trend["benchmark"]["observations"]), 20)
        self.assertIsNone(trend["relative_return_pp"])
        md, _, _ = self._full_detail_download(resp.json())
        # 板块实际观测（19 行）+ 覆盖进正文（首日缺失行不在板块表内；
        # 基准表 20 行完整含该日期——两侧独立）
        self.assertIn("板块合格观测：19/20", md)
        self.assertIn(f"| {expected[1].isoformat()} | 101.0 |", md)
        self.assertNotIn(f"| {expected[0].isoformat()} | 1000.0 |", md)
        # 合格基准不因板块残缺隐藏：收益与观测表进正文
        self.assertIn("同窗口区间收益：", md)
        self.assertIn("| 日期 | 沪深 300 收盘（指数点位） |", md)
        self.assertIn(f"| {expected[0].isoformat()} | 4000.0 |", md)
        # 相对差不输出伪值，并说明需双侧合格（板块侧不合格原因见缺口）
        self.assertIn("相对表现：不计算", md)

    def test_peer_change_missing_no_full_sample_rank(self):
        """同行涨跌幅缺失：不发全样本名次（不伪 1/6），有效比较范围缺口进正文。"""
        expected = self._real_window()
        cons = _cons_df(["600001", "600002", "600003", "600004", "600005", "600519"])
        cons.loc[cons["代码"] != "600519", "涨跌幅"] = float("nan")
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        self.assertEqual(peers["sample_size"], 6)
        self.assertIsNone(peers["target_change_pct_rank"])  # 不伪「第 1 / 6」
        coverage = [g for g in peers["gaps"] if "有效比较范围" in g]
        self.assertTrue(coverage, peers["gaps"])
        self.assertIn("1/6", coverage[0])
        md, _, _ = self._full_detail_download(resp.json())
        self.assertNotIn("排第 **1** / 6", md)
        self.assertIn("有效比较范围 1/6", md)  # 缺口在正文可见
        self.assertIn("不输出全样本名次", md)

    def test_peer_amount_invalid_gap_with_six_rankable(self):
        """最大成交额行 NaN 后仍有 6 家可排名：剔除限制不被剩余行数吞掉，进正文。"""
        expected = self._real_window()
        cons = _cons_df(["600001", "600002", "600003", "600004", "600005",
                         "600006", "600519"])
        cons.loc[0, "成交额"] = float("nan")  # 最大成交额行无效
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        self.assertEqual(peers["sample_size"], 6)  # 5 家可排名 + 目标殿后
        self.assertEqual(peers["rankable_rows"], 6)
        self.assertEqual(peers["snapshot_rows"], 7)
        self.assertTrue(any("成交额无效" in g for g in peers["gaps"]), peers["gaps"])
        codes = [i["code"] for i in peers["items"]]
        self.assertEqual(codes[:5], ["600002", "600003", "600004", "600005", "600006"])
        self.assertEqual(codes[5], "600519")
        md, _, _ = self._full_detail_download(resp.json())
        self.assertIn("成交额无效", md)  # 限制在正文可见（旧版 JSON-only）
        self.assertIn("不参与成交额排名", md)

    def test_target_amount_missing_retained_member_not_500(self):
        """目标股成交额 NaN：成员保留 + top5 资格 None + 缺口，不整丢不误报。"""
        expected = self._real_window()
        cons = _cons_df(["600001", "600002", "600003", "600004", "600005",
                         "600006", "600519"])
        cons.loc[cons["代码"] == "600519", "成交额"] = float("nan")
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        targets = [i for i in peers["items"] if i["is_target"]]
        self.assertEqual(len(targets), 1)  # 目标成员保留
        self.assertIsNone(targets[0]["amount"])
        self.assertIsNone(peers["target_in_amount_top5"])  # 无法判定，不误报 False
        self.assertTrue(any("成员保留" in g for g in peers["gaps"]))
        md, _, _ = self._full_detail_download(resp.json())
        self.assertIn("不参与成交额排名", md)
        self.assertIn("成员保留", md)

    def test_blank_peer_name_adapts_to_code_not_500(self):
        """空白供应商名称：代码显示 + 缺口，其余有效报告存活（旧版整篇 500）。"""
        expected = self._real_window()
        cons = _cons_df(["600001", "600002", "600003", "600004", "600005", "600519"])
        cons.loc[cons["代码"] == "600001", "名称"] = ""
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)  # 不再 500
        peers = resp.json()["analysis"]["sector_peers"]
        blank = next(i for i in peers["items"] if i["code"] == "600001")
        self.assertEqual(blank["name"], "600001")  # 代码显示
        self.assertTrue(any("名称空白" in g for g in peers["gaps"]))
        md, _, _ = self._full_detail_download(resp.json())
        self.assertIn("名称空白", md)

    def test_wrong_shape_history_is_source_gap_not_500(self):
        """供应商日线错误形状（dict）：源错误降块记缺口，HTTP 200 不再 500。"""
        expected = self._real_window()
        resp = self._generate_confirmed(
            board_hist={"日期": ["2026-10-09"], "收盘": [1000.0]},  # type: ignore[arg-type]
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=_cons_df(["600519", "000568"]))
        self.assertEqual(resp.status_code, 200, resp.text)  # 不再 AttributeError → 500
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertIsNone(trend["period_return_pct"])
        self.assertEqual(trend["observations"], [])
        self.assertTrue(any("形状异常" in g for g in trend["gaps"]))
        self.assertIsNotNone(trend["benchmark"]["period_return_pct"])  # 基准独立保留
        md, _, _ = self._full_detail_download(resp.json())
        self.assertIn("形状异常", md)

    def test_duplicate_code_gap_visible_in_markdown(self):
        """重复代码剔除：缺口不只在 JSON，正文/下载同样披露（spec P1#3）。"""
        expected = self._real_window()
        cons = _cons_df(["600001", "600002", "600003", "600004", "600005", "600519"])
        cons = pd.concat([cons, cons.iloc[:1]], ignore_index=True)
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected),
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        peers = resp.json()["analysis"]["sector_peers"]
        codes = [i["code"] for i in peers["items"]]
        self.assertNotIn("600001", codes)
        self.assertTrue(any("重复代码" in g for g in peers["gaps"]))
        md, _, _ = self._full_detail_download(resp.json())
        self.assertIn("重复代码", md)  # 正文可见

    def test_combined_many_gaps_http200_no_500(self):
        """组合多缺口（板块缺天+双源不合格+金额 NaN+涨跌幅 NaN+重复代码+空白名）：
        schema 边界不炸（gaps 有界），HTTP 200 + 各块缺口如实。"""
        expected = self._real_window()
        cons = _cons_df(["600001", "600002", "600003", "600004", "600005",
                         "600006", "600007", "600519"])
        cons.loc[0, "成交额"] = float("nan")       # 最大额无效
        cons.loc[1, "涨跌幅"] = float("nan")       # 一家涨跌幅缺失
        cons.loc[3, "名称"] = "  "                 # 空白名称（600004，不与重复冲突叠加）
        dup = pd.DataFrame([{"代码": "600003", "名称": "股票600003乙",
                             "涨跌幅": 9.9, "成交额": 1e10}])
        cons = pd.concat([cons, dup], ignore_index=True)
        board_hist = _daily_hist_df(expected).iloc[1:]   # 板块缺 1 天
        bench_hist = _daily_hist_df(expected, base=4000.0, step=1.0,
                                    date_col="date", close_col="close").iloc[1:]  # 基准缺 1 天
        resp = self._generate_confirmed(board_hist=board_hist, bench_hist=bench_hist,
                                        cons_df=cons)
        self.assertEqual(resp.status_code, 200, resp.text)
        analysis = resp.json()["analysis"]
        trend, peers = analysis["sector_trend"], analysis["sector_peers"]
        self.assertLessEqual(len(trend["gaps"]), 10)
        self.assertLessEqual(len(peers["gaps"]), 14)
        self.assertLessEqual(len(analysis["assessment"]["reasons"]), 10)
        self.assertLessEqual(len(analysis["gaps"]), 20)
        md, _, _ = self._full_detail_download(resp.json())
        self.assertIn("板块合格观测：19/20", md)  # 部分观测与覆盖如实
        self.assertIn("成交额无效", md)
        self.assertIn("名称空白", md)
        self.assertIn("重复代码", md)

    # ---- issue #82 第二次独立审查四个 P2 反例（公开 API 主缝回归）----

    def test_benchmark_partial19_renders_rows_in_markdown(self):
        """板块 20/20 + 基准 19/20：基准 19 条实际观测与覆盖独立进正文
        （审查反例：观测表曾嵌在收益分支，正文 0 行）。"""
        expected = self._real_window()
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected, base=1000.0, step=1.0),
            bench_hist=_daily_hist_df(expected, base=4000.0, step=1.0,
                                      date_col="date", close_col="close").iloc[1:],
            cons_df=_cons_df(["600519", "000568"]))
        self.assertEqual(resp.status_code, 200, resp.text)
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertIsNotNone(trend["period_return_pct"])  # 板块侧独立合格
        self.assertEqual(trend["benchmark"]["qualified_sessions"], 19)
        self.assertEqual(len(trend["benchmark"]["observations"]), 19)
        self.assertIsNone(trend["benchmark"]["period_return_pct"])
        self.assertIsNone(trend["relative_return_pp"])
        md, archived, _ = self._full_detail_download(resp.json())
        # 基准覆盖与观测表独立于收益渲染：19 行全部进正文
        self.assertIn("基准（沪深 300）：同窗口合格观测 19/20 个交易日", md)
        self.assertIn(f"| {expected[1].isoformat()} | 4001.0 |", md)
        self.assertIn(f"| {expected[19].isoformat()} | 4019.0 |", md)
        self.assertNotIn(f"| {expected[0].isoformat()} | 4000.0 |", md)
        self.assertIn("本窗口区间收益不合格或未取得", md)
        self.assertIn("相对表现不计算", md)
        self.assertNotIn("同窗口区间收益", md)  # 基准侧无 20/20 不给收益
        # 存档 JSON 与正文一致（19 条观测实际持久化）
        self.assertEqual(len(archived["sector_trend"]["benchmark"]["observations"]), 19)

    def test_both_partial_render_independent_tables(self):
        """双 partial（各 19/20）：两侧观测表与覆盖都进正文，互不隐藏、无伪收益。"""
        expected = self._real_window()
        resp = self._generate_confirmed(
            board_hist=_daily_hist_df(expected, base=1000.0, step=1.0).iloc[1:],
            bench_hist=_daily_hist_df(expected, base=4000.0, step=1.0,
                                      date_col="date", close_col="close").iloc[1:],
            cons_df=_cons_df(["600519", "000568"]))
        self.assertEqual(resp.status_code, 200, resp.text)
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertEqual(trend["qualified_sessions"], 19)
        self.assertEqual(trend["benchmark"]["qualified_sessions"], 19)
        self.assertIsNone(trend["relative_return_pp"])
        md, _, _ = self._full_detail_download(resp.json())
        self.assertIn("板块合格观测：19/20", md)
        self.assertIn("同窗口合格观测 19/20", md)
        self.assertIn("| 日期 | 板块收盘（板块指数点位） |", md)
        self.assertIn("| 日期 | 沪深 300 收盘（指数点位） |", md)
        self.assertNotIn("板块区间收益", md)
        self.assertNotIn("同窗口区间收益", md)

    def test_duplicate_date_column_http200_source_gap_not_user_400(self):
        """日期列重复的畸形 DataFrame：源形状缺口降块，HTTP 200 + 基准/同业保留
        （审查反例：ValueError 逃出边界后被 endpoint 误报用户 400、零存档）。"""
        expected = self._real_window()
        board_hist = _daily_hist_df(expected)
        board_hist = pd.concat([board_hist, board_hist[["日期"]]], axis=1)
        resp = self._generate_confirmed(
            board_hist=board_hist,
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=_cons_df(["600519", "000568"]))
        self.assertEqual(resp.status_code, 200, resp.text)  # 不再 400
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertIsNone(trend["period_return_pct"])
        self.assertEqual(trend["observations"], [])
        self.assertTrue(any("形状异常" in g for g in trend["gaps"]))
        self.assertIsNotNone(trend["benchmark"]["period_return_pct"])  # 基准独立保留
        peers = resp.json()["analysis"]["sector_peers"]
        self.assertTrue(peers["items"])  # 同业不受拖累
        md, archived, _ = self._full_detail_download(resp.json())
        self.assertIn("形状异常", md)
        history = self.client.get("/api/v1/sector-analysis/reports")
        self.assertEqual(history.status_code, 200)
        self.assertTrue(any(r["id"] == resp.json()["report_id"]
                            for r in history.json()["data"]))  # 部分报告已存档

    def test_five_duplicate_dates_http200_archived_not_500(self):
        """valid20 + 重复前 5 天：长缺口有界，assessment.reasons ≤200，
        HTTP 200 + 完整存档（审查反例：string_too_long → 500 零存档）。"""
        expected = self._real_window()
        board_hist = pd.concat([_daily_hist_df(expected),
                                _daily_hist_df(expected)[:5]], ignore_index=True)
        resp = self._generate_confirmed(
            board_hist=board_hist,
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=_cons_df(["600519", "000568"]))
        self.assertEqual(resp.status_code, 200, resp.text)  # 不再 500/零存档
        analysis = resp.json()["analysis"]
        trend = analysis["sector_trend"]
        for g in trend["gaps"]:
            self.assertLessEqual(len(g), 200, g)
        for r in analysis["assessment"]["reasons"]:
            self.assertLessEqual(len(r), 200, r)
        dup_gaps = [g for g in trend["gaps"] if "重复日期冲突" in g]
        self.assertTrue(dup_gaps, trend["gaps"])
        self.assertNotIn("datetime.date(", dup_gaps[0])  # 紧凑 isoformat
        self.assertIn("5 天重复日期冲突", dup_gaps[0])
        # 合格基准与同业一起保留（不随板块缺口丢失）
        self.assertEqual(len(trend["benchmark"]["observations"]), 20)
        self.assertIsNotNone(trend["benchmark"]["period_return_pct"])
        self.assertEqual(analysis["sector_peers"]["sample_size"], 2)
        md, _, _ = self._full_detail_download(resp.json())
        self.assertIn("重复日期冲突", md)
        self.assertEqual(analysis["assessment"]["completeness"], "partial")

    def test_compound_dup_invalid_missing_independent_gaps_api(self):
        """复合源缺陷（缺 1 天 + 1 天收盘 0 + 1 天重复）：三类缺口独立可见进正文，
        重复缺口不掩盖其他缺失（审查反例：唯一缺口只说重复日）。"""
        expected = self._real_window()
        board_hist = _daily_hist_df(expected).iloc[1:].copy()  # 缺最早一天
        board_hist.loc[board_hist.index[0], "收盘"] = 0.0      # 次日收盘无效
        board_hist = pd.concat([board_hist, board_hist.iloc[3:4]],
                               ignore_index=True)               # 重复 expected[4]
        resp = self._generate_confirmed(
            board_hist=board_hist,
            bench_hist=_daily_hist_df(expected, date_col="date", close_col="close"),
            cons_df=_cons_df(["600519", "000568"]))
        self.assertEqual(resp.status_code, 200, resp.text)
        trend = resp.json()["analysis"]["sector_trend"]
        self.assertEqual(trend["qualified_sessions"], 17)
        self.assertTrue(any("重复日期冲突" in g and expected[4].isoformat() in g
                            for g in trend["gaps"]), trend["gaps"])
        self.assertTrue(any("收盘无效或非正" in g and expected[1].isoformat() in g
                            for g in trend["gaps"]), trend["gaps"])
        self.assertTrue(any("缺失 1 天" in g and expected[0].isoformat() in g
                            for g in trend["gaps"]), trend["gaps"])
        md, archived, _ = self._full_detail_download(resp.json())
        self.assertIn("重复日期冲突", md)
        self.assertIn("收盘无效或非正", md)
        self.assertIn("缺失 1 天", md)
        self.assertEqual(len(archived["sector_trend"]["observations"]), 17)


class TestLivePipeTitleTableEscape(SectorReportApiV2TestCase):
    """issue #83 live 表格标题分隔符（真实 sa_202611061255 cat-2 形状）。

    真实标题含字面 ``|``（163.com 联播日日评真实文章）：经公开 POST 全链路
    （真实准入/资格/登记表/引用校验/渲染/临时 SQLite 存档）后，催化剂表
    （provider 与 article 两条 date_basis 路径）与背景表都保持 GFM 5 列
    结构与列语义（日期/口径/来源各归其位、真实来源域名不丢），原始链接
    href+标题字面可见；generate/detail/download 三口一致；payload JSON 中
    标题原文不动。终端隔离纪律不变：搜索/模型/文章日期终端受控，出网守卫
    保持（文章日期终端按既有契约隔离，真实 HTTP 抓取链路由
    test_sector_article_fetch_bounds 的回环实测覆盖）。
    """

    LIVE_TITLE = '央行重申适度宽松政策，白酒行业持续去库存| 财经日日评'
    LIVE_URL = 'https://www.163.com/dy/article/LB1OOAJ005199A0B.html'
    BG_TITLE = '白酒板块资金流向观察| 每日复盘'
    BG_URL = 'https://finance.eastmoney.com/a/202611023918912943.html'

    def _post_with_live_results(self, results: list[dict]) -> dict:
        """合成搜索终端（真实 `|` 形状标题）+ 完整 confirmed 供应商基座 → POST。"""
        from src.services import sector_research
        self._install_research_success(results=results)
        expected = recent_completed_trading_sessions("cn", 20)
        board_hist = _daily_hist_df(expected, base=1000.0, step=1.0)
        bench_hist = _daily_hist_df(expected, base=4000.0, step=1.0,
                                    date_col="date", close_col="close")
        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"], board_table=boards,
            board_hist=board_hist, bench_hist=bench_hist)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _probe_no_evidence(self, *_args, **_kwargs):
        from src.services import sector_research
        return sector_research.ArticleDateEvidence(None, None, None), None, None

    def _detail_download_md(self, body: dict) -> tuple[dict, str]:
        """详情/下载回读并断言三口一致：返回 (存档 analysis_json, 下载原文)。"""
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        dl = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(detail.json()["data"]["markdown"], body["markdown"])
        return detail.json()["data"]["analysis_json"], dl.text

    def test_provider_and_background_pipe_titles_keep_gfm_columns(self):
        from src.services import sector_research
        from tests.test_report_formatting import _gfm_table_rows

        today = datetime.now().date().isoformat()
        results = [
            {"title": self.LIVE_TITLE,
             "snippet": "白酒行业去库存持续推进，政策面保持适度宽松",
             "url": self.LIVE_URL, "source": "www.163.com",
             "published_date": today},
            {"title": self.BG_TITLE,
             "snippet": "白酒板块资金动向",
             "url": self.BG_URL, "source": "finance.eastmoney.com",
             "published_date": None},
        ]
        with patch.object(self._svc, "_probe_article_date_evidence",
                          self._probe_no_evidence):
            body = self._post_with_live_results(results)
        cat = body["analysis"]["sector_catalysts"]
        # provider 路径：JSON 载荷标题原样（渲染层转义不污染数据层）
        self.assertEqual(len(cat["items"]), 1)
        self.assertEqual(cat["items"][0]["date_basis"], "provider_published")
        self.assertEqual(cat["items"][0]["title"], self.LIVE_TITLE)
        self.assertEqual(len(cat["background_items"]), 1)
        self.assertEqual(cat["background_items"][0]["title"], self.BG_TITLE)
        archived, dl = self._detail_download_md(body)
        md = body["markdown"]
        self.assertEqual(archived["sector_catalysts"]["items"][0]["title"],
                         self.LIVE_TITLE)
        rows, links = _gfm_table_rows(md)
        header = rows.index(["证据 ID", "标题（链接为原始引用）", "发布日期",
                             "日期口径", "来源"])
        # provider 行：5 列、日期/口径/来源各归其位（真实来源域名不丢）
        self.assertEqual(rows[header + 1],
                         ["cat-1", self.LIVE_TITLE, today, "供应商发布字段",
                          "www.163.com"])
        bg_header = rows.index(["证据 ID", "标题（链接为原始引用）", "发布日期",
                                "来源", "限制说明"])
        # 背景行：5 列、限制说明在末列（未左移）
        self.assertEqual(rows[bg_header + 1],
                         ["bg-1", self.BG_TITLE, "未知",
                          "finance.eastmoney.com", "文章无可用发布日期证据"])
        # 原始链接 href + 标题字面文本（含 ``|``）可见；下载原文与生成一致
        self.assertIn((self.LIVE_URL, self.LIVE_TITLE), links)
        self.assertIn((self.BG_URL, self.BG_TITLE), links)
        self.assertIn("去库存\\| 财经日日评", md)  # 定界符已字面化（渲染投影）
        self.assertEqual(dl, md)

    def test_article_metadata_pipe_title_keeps_gfm_columns_after_upgrade(self):
        from tests.test_report_formatting import _gfm_table_rows

        today = datetime.now().date()
        meta = (f'<meta property="article:published_time" '
                f'content="{today.isoformat()}T08:33:03+08:00">')

        def _probe_meta_evidence(*_args, **_kwargs):
            from src.services import sector_research
            return (sector_research.ArticleDateEvidence(
                metadata_date=today, url_declared_date=None,
                raw_basis=meta), None, None)

        results = [{"title": self.LIVE_TITLE,
                    "snippet": "白酒行业去库存持续推进",
                    "url": self.LIVE_URL, "source": "www.163.com",
                    "published_date": None}]
        with patch.object(self._svc, "_probe_article_date_evidence",
                          _probe_meta_evidence):
            body = self._post_with_live_results(results)
        cat = body["analysis"]["sector_catalysts"]
        # article 路径：无日期候选经真实升级链（资格判定+登记表）成为已证实催化
        self.assertEqual(len(cat["items"]), 1)
        self.assertEqual(cat["items"][0]["date_basis"], "article_metadata")
        self.assertEqual(cat["items"][0]["published_date"], today.isoformat())
        self.assertEqual(cat["items"][0]["title"], self.LIVE_TITLE)
        archived, dl = self._detail_download_md(body)
        md = body["markdown"]
        rows, links = _gfm_table_rows(md)
        header = rows.index(["证据 ID", "标题（链接为原始引用）", "发布日期",
                             "日期口径", "来源"])
        self.assertEqual(rows[header + 1],
                         ["cat-1", self.LIVE_TITLE, today.isoformat(),
                          "文章元数据（含原始依据）", "www.163.com"])
        self.assertIn((self.LIVE_URL, self.LIVE_TITLE), links)
        self.assertEqual(dl, md)

    def test_bracket_backslash_titles_whole_anchor_across_paths(self):
        """root 反例：反斜杠+括号标题在两条 date_basis 与背景路径均整体入链。

        反斜杠加倍若不 literalize 链接文本括号，``\\]`` 奇偶改变使 adopted
        URL anchor 由 1 变 0、``\\[`` 使标题前缀脱落链接外。经真实 POST 全
        链路（准入/资格/登记表/渲染/临时 SQLite）断言完整标题在唯一原始
        href anchor 内、5 列结构与日期/来源归位、JSON 原文不动、三口一致。
        """
        from src.services import sector_research
        from tests.test_report_formatting import _gfm_table_rows

        today = datetime.now().date()
        meta = (f'<meta property="article:published_time" '
                f'content="{today.isoformat()}T08:33:03+08:00">')
        anchor_broken = '政策\\]解读'          # 反例1：旧 filter 下 anchor 1→0
        prefix_dropped = '政策\\[解读| 资讯'   # 反例2：旧 filter 下前缀出链
        bg_bracket = '白酒板块观察| 盘点]'      # 背景 + 竖线/闭括号形状

        upgraded_url = "https://www.163.com/dy/article/BRACKETTEST1.html"

        def _probe_by_url(url, *_args, **_kwargs):
            if url in (self.LIVE_URL, upgraded_url):
                return (sector_research.ArticleDateEvidence(
                    metadata_date=today, url_declared_date=None,
                    raw_basis=meta), None, None)
            return (sector_research.ArticleDateEvidence(
                None, None, None), None, None)

        results = [
            {"title": anchor_broken,
             "snippet": "白酒行业政策解读，去库存推进",
             "url": self.LIVE_URL, "source": "www.163.com",
             "published_date": today.isoformat()},
            {"title": prefix_dropped,
             "snippet": "白酒行业资讯汇总",
             "url": upgraded_url, "source": "www.163.com",
             "published_date": None},
            {"title": bg_bracket,
             "snippet": "白酒板块观察记录",
             "url": "https://finance.eastmoney.com/a/observe.html",
             "source": "finance.eastmoney.com",
             "published_date": None},
        ]
        with patch.object(self._svc, "_probe_article_date_evidence",
                          _probe_by_url):
            body = self._post_with_live_results(results)
        cat = body["analysis"]["sector_catalysts"]
        # JSON 原文不动：provider 与 article 两条路径、背景标题逐字保持
        self.assertEqual([i["title"] for i in cat["items"]],
                         [anchor_broken, prefix_dropped])
        self.assertEqual([i["date_basis"] for i in cat["items"]],
                         ["provider_published", "article_metadata"])
        self.assertEqual(cat["background_items"][0]["title"], bg_bracket)
        archived, dl = self._detail_download_md(body)
        self.assertEqual(archived["sector_catalysts"]["items"][0]["title"],
                         anchor_broken)
        md = body["markdown"]
        rows, links = _gfm_table_rows(md)
        header = rows.index(["证据 ID", "标题（链接为原始引用）", "发布日期",
                             "日期口径", "来源"])
        self.assertEqual(rows[header + 1],
                         ["cat-1", anchor_broken, today.isoformat(),
                          "供应商发布字段", "www.163.com"])
        self.assertEqual(rows[header + 2],
                         ["cat-2", prefix_dropped, today.isoformat(),
                          "文章元数据（含原始依据）", "www.163.com"])
        bg_header = rows.index(["证据 ID", "标题（链接为原始引用）", "发布日期",
                                "来源", "限制说明"])
        # prefix_dropped 无日期时先以 bg-1 登记、升级成功后移出背景，
        # 背景列表仅剩 bg_bracket（编号顺延为 bg-2）
        self.assertEqual(rows[bg_header + 1],
                         ["bg-2", bg_bracket, "未知",
                          "finance.eastmoney.com", "文章无可用发布日期证据"])
        # whole anchor：完整标题文本恰在唯一 adopted href anchor 内
        self.assertIn((self.LIVE_URL, anchor_broken), links)
        self.assertIn((upgraded_url, prefix_dropped), links)
        self.assertIn(("https://finance.eastmoney.com/a/observe.html",
                       bg_bracket), links)
        self.assertEqual(dl, md)


class TestSynthesisPromptFidelity(SectorReportApiV2TestCase):
    """final user prompt 曾裸重复带禁字符的输入身份 label（真实 600036/BK0475
    银行报告观察：3 accepted + 2 rejected 共享 reason Ⅱ → truthful partial；
    被拒结论的原始文本未保留，具体措辞与成因无法确证——重复 label 是可能的
    复制风险，不是已确立根因）。

    对齐后经**真实模型 wire**（合成终端响应，捕获实际请求体）观察：
    身份事实原样保留在【已确认事实】identity JSON（不清洗输入、不改板块
    label），裸 label 叙述行不再出现；strict 校验/rejected→partial/存档/
    读回行为不变。合成 wire 只证明输入身份与校验链路逐字保持，不证明实际
    模型行为已改变（live 效果归 root 真实报告验收）。不加机械匹配新 prompt
    句子的断言。
    """

    def test_final_user_prompt_carries_identity_without_bare_label_repeat(self):
        import httpx as _httpx
        import json as _json

        today = datetime.now().date().isoformat()
        self._install_research_success(results=[{
            "title": "白酒行业新政策发布",
            "snippet": "监管部门发布白酒行业新规，行业迎来政策催化",
            "url": "https://example.com/news/baijiu.html",
            "source": "example.com",
            "published_date": today,
        }])
        expected = recent_completed_trading_sessions("cn", 20)
        board_hist = _daily_hist_df(expected, base=1000.0, step=1.0)
        bench_hist = _daily_hist_df(expected, base=4000.0, step=1.0,
                                    date_col="date", close_col="close")
        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"], board_table=boards,
            board_hist=board_hist, bench_hist=bench_hist)
        # 外层 wire 捕获（后 patch 先执行 → 先捕获再透传给合成终端响应）
        orig_send = _httpx.AsyncClient.send
        captured: list[bytes] = []

        async def _capture_send(client_self, request, *args, **kwargs):
            captured.append(request.content)
            return await orig_send(client_self, request, *args, **kwargs)

        wire_patcher = patch.object(_httpx.AsyncClient, "send", _capture_send)
        wire_patcher.start()
        self.addCleanup(wire_patcher.stop)

        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(captured, "模型出站 wire 未捕获（合成调用应发生）")
        wire_body = _json.loads(captured[-1].decode("utf-8"))
        messages = wire_body["messages"]
        self.assertEqual(messages[0]["role"], "system")
        user_text = messages[-1]["content"]
        # 对齐后的可观察变化：裸 label 叙述行不再重复身份（旧 prompt 确切
        # 特征消失；成因推断不在断言范围，见类 docstring）
        self.assertNotIn("目标行业：", user_text)
        # 身份事实原样保留在 facts JSON（不清洗输入、不改板块 label）
        self.assertIn("【已确认事实】", user_text)
        self.assertIn("600519", user_text)
        self.assertIn("白酒Ⅱ", user_text)
        self.assertIn("BK1277", user_text)
        # 既有 strict 定性约束与 root-decision 输出边界仍在 system prompt
        # （单句存在性，非逐句镜像）
        self.assertIn("不得出现任何数量字符", messages[0]["content"])
        self.assertIn("市场事实已由服务端以定性观察关系提供",
                      messages[0]["content"])
        # 生成行为不变：claims 过 strict 校验、rejected→partial 语义与
        # 存档/读回链路照常（本用例 claims 干净 → ok/complete 不受影响）；
        # summary 为服务端程序化事实摘要（server origin）
        analysis = resp.json()["analysis"]
        self.assertEqual(analysis["sector_synthesis"]["status"], "ok")
        self.assertEqual(analysis["sector_synthesis"]["summary_origin"],
                         "server_facts")
        rid = resp.json()["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["markdown"], resp.json()["markdown"])


class TestMetricSemanticsInput(SectorReportApiV2TestCase):
    """真实 sa_202611061429 语义验收反例的 input-contract 修复（root 语义审查）。

    registry 投影曾只给裸数值：intraday 无 momentum/breadth 口径说明、peers
    丢弃 selection_rule、facts_json 不带语义标题——模型无从知道 23.8 是横截
    面分位而非带符号动量、peers 是成交额活跃代表而非龙头。本用例经公开 POST
    → 真实 adapter wire（合成终端响应，捕获实际请求体）→ strict 存档/读回：
    - 完整原始事实（负日收益 + 正分位、成交额选取的活跃同业、无任何资金流
      字段）连同口径语义到达配置模型 seam；
    - 无数量字符的定性 risk 通过 strict 校验（status ok）；
    - mixed invalid（含数字陈述）被整条拒绝、rejected→partial、存档读回一致。
    合成响应不证明真实模型遵守 prompt（live 归 root 一次真实验收）。
    """

    def _weak_board_table(self) -> "pd.DataFrame":
        """root 反例形状：目标板块负日收益 + 正低分位 + 低上涨占比。"""
        rows = [("白酒Ⅱ", "BK1277", -1.9, 1, 19), ("电池", "BK1033", 0.5, 12, 8),
                ("半导体", "BK0917", 3.1, 15, 5), ("银行", "BK0475", 0.8, 10, 10),
                ("煤炭", "BK0437", -5.0, 2, 18), ("医药", "BK0464", -2.5, 6, 14),
                ("汽车", "BK0481", 2.0, 14, 6), ("食品饮料", "BK0438", 1.2, 13, 7),
                ("电子", "BK1032", -0.3, 9, 11), ("计算机", "BK1036", 0.2, 11, 9)]
        return pd.DataFrame({
            "板块名称": [r[0] for r in rows], "板块代码": [r[1] for r in rows],
            "涨跌幅": [r[2] for r in rows], "换手率": [2.0] * len(rows),
            "成交额": [1.0e9] * len(rows), "上涨家数": [r[3] for r in rows],
            "下跌家数": [r[4] for r in rows], "总市值": [1.0e12] * len(rows),
            "领涨股票": ["样本股"] * len(rows),
            "领涨股票-涨跌幅": [5.0] * len(rows)})

    def _post_weak_snapshot(self, model_claims, cons_df=None):
        self._install_research_success(model_claims=model_claims)
        expected = recent_completed_trading_sessions("cn", 20)
        board_hist = _daily_hist_df(expected, base=1000.0, step=1.0)
        bench_hist = _daily_hist_df(expected, base=4000.0, step=1.0,
                                    date_col="date", close_col="close")
        self._mock_vendors(
            industry="白酒Ⅱ", boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"], cons_df=cons_df,
            board_table=self._weak_board_table(), board_hist=board_hist,
            bench_hist=bench_hist)
        import httpx as _httpx

        orig_send = _httpx.AsyncClient.send
        captured: list[bytes] = []

        async def _capture_send(client_self, request, *args, **kwargs):
            captured.append(request.content)
            return await orig_send(client_self, request, *args, **kwargs)

        wire_patcher = patch.object(_httpx.AsyncClient, "send", _capture_send)
        wire_patcher.start()
        self.addCleanup(wire_patcher.stop)
        resp = self.client.post(
            "/api/v1/sector-analysis/generate?stock_code=600519&stock_name=贵州茅台"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json(), captured

    def _user_text(self, captured) -> "tuple[str, str]":
        import json as _json

        wire_body = _json.loads(captured[-1].decode("utf-8"))
        messages = wire_body["messages"]
        self.assertEqual(messages[0]["role"], "system")
        return messages[-1]["content"], messages[0]["content"]

    def test_metric_ownership_wire_and_program_summary_valid_risk_passes(self):
        claims = [{"kind": "risk",
                   "text": "政策催化尚处推进阶段，若落地节奏不及预期，"
                           "前期情绪修复存在回吐风险，持续性有待验证",
                   "evidence_refs": ["intraday", "peers", "target", "cat-1"]}]
        body, captured = self._post_weak_snapshot(claims)
        user_text, system_text = self._user_text(captured)
        # root-decision：raw market magnitudes 有意排除出模型投影
        # （-1.9=当日涨跌幅、22.2=分位；identity/新闻数值仍保留）
        self.assertNotIn("-1.9", user_text)
        self.assertNotIn("22.2", user_text)
        # 市场事实以服务端定性观察关系到达 seam（符号/单期范围/选取口径）
        self.assertIn("【市场事实观察】", user_text)
        self.assertIn("板块指数当日下跌", user_text)
        self.assertIn("单期快照", user_text)
        self.assertIn("横截面相对位置", user_text)
        self.assertIn("成交额前五", user_text)
        self.assertIn("方向一致", user_text)
        # identity 与催化剂/新闻 provenance 原样保留
        self.assertIn("600519", user_text)
        self.assertIn("白酒Ⅱ", user_text)
        self.assertIn("published_date", user_text)
        # 输出边界规则在 system prompt（单句存在性，非逐句镜像）
        self.assertIn("市场事实已由服务端以定性观察关系提供", system_text)
        # 程序化事实摘要：精确值进 summary（server 拥有数值关系），claims
        # 不参与摘要
        analysis = body["analysis"]
        syn = analysis["sector_synthesis"]
        self.assertEqual(syn["status"], "ok")
        self.assertEqual(syn["rejected_count"], 0)
        self.assertEqual(syn["summary_origin"], "server_facts")
        self.assertIn("-1.9%", syn["summary"])
        self.assertIn("8/10", syn["summary"])
        self.assertIn("22.2/100", syn["summary"])
        self.assertIn("5.0%", syn["summary"])
        self.assertNotIn("偏高", syn["summary"])
        # 观察限制（单期范围/非资金流/非龙头分类）在 uncertainty_note
        self.assertIn("单期快照", syn["uncertainty_note"] or "")
        # valid 定性 risk 通过 strict 校验；generate/detail/download 三口一致
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(detail.json()["data"]["markdown"], body["markdown"])
        self.assertEqual(download.text, body["markdown"])
        # 存档 origin 与摘要逐字一致
        archived = detail.json()["data"]["analysis_json"]
        if isinstance(archived, str):
            archived = json.loads(archived)
        self.assertEqual(archived["sector_synthesis"]["summary_origin"],
                         "server_facts")
        self.assertEqual(archived["sector_synthesis"]["summary"],
                         syn["summary"])

    def test_mixed_invalid_quantitative_claim_stays_partial(self):
        wrong_clause = "板块当日动量分位为22.2，显示资金偏好仍在延续"
        claims = [
            {"kind": "risk",
             "text": "政策催化尚处推进阶段，短期情绪承压且持续性有待验证",
             "evidence_refs": ["intraday", "peers", "cat-1"]},
            {"kind": "interpretation",
             "text": wrong_clause,
             "evidence_refs": ["intraday"]},
        ]
        body, _captured = self._post_weak_snapshot(claims)
        syn = body["analysis"]["sector_synthesis"]
        # 含数字陈述被 strict 校验整条拒绝；合格结论与事实保留，如实 partial
        self.assertEqual(syn["status"], "ok")
        self.assertEqual(syn["rejected_count"], 1)
        # 完整性状态直接断言（generate 响应与存档两处）
        self.assertEqual(body["analysis"]["assessment"]["completeness"],
                         "partial")
        # root-decision：程序化核心字段不消费模型文本——错误句（含数字被拒
        # 或纯定性错误）都不可能进入 summary/uncertainty
        self.assertNotIn(wrong_clause, syn["summary"] or "")
        self.assertNotIn(wrong_clause, syn["uncertainty_note"] or "")
        self.assertEqual(syn["summary_origin"], "server_facts")
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        archived = detail.json()["data"]["analysis_json"]
        if isinstance(archived, str):
            archived = json.loads(archived)
        self.assertEqual(archived["sector_synthesis"]["rejected_count"], 1)
        self.assertEqual(archived["assessment"]["completeness"], "partial")
        self.assertNotIn(wrong_clause,
                         archived["sector_synthesis"]["summary"] or "")
        # generate/detail/download 三口一致
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(detail.json()["data"]["markdown"], body["markdown"])
        self.assertEqual(download.text, body["markdown"])

    def test_adversarial_qualitative_wrong_clause_never_enters_core_fields(self):
        """1449 反例（sa_202611061449）形状：纯定性错误序数句（无数量字符，
        strict 校验无法拦截）——owner 边界保证其不进入 summary/uncertainty。

        该 claim 在 claims 列表中单独立示（模型 origin 可见），报告语义
        仍属人类验收 FAIL 范畴——本断言证明核心字段封闭，不声称修复模型
        行为（live 归 root 一次真实验收）。"""
        wrong = ("不过横截面动量分位仍处于相对偏高区间，需注意该分位仅为"
                 "单期快照的横截面相对水平")
        claims = [
            {"kind": "counterevidence",
             "text": wrong,
             "evidence_refs": ["intraday"]},
            {"kind": "risk",
             "text": "政策催化尚处推进阶段，若落地节奏不及预期，"
                     "短期情绪承压且持续性有待验证",
             "evidence_refs": ["intraday", "cat-1"]},
        ]
        body, _captured = self._post_weak_snapshot(claims)
        syn = body["analysis"]["sector_synthesis"]
        self.assertEqual(syn["status"], "ok")
        self.assertEqual(syn["rejected_count"], 0)
        # 错误句不在两个核心字段（不是前缀化，是缺席）
        self.assertNotIn(wrong, syn["summary"] or "")
        self.assertNotIn(wrong, syn["uncertainty_note"] or "")
        self.assertNotIn("相对偏高", syn["summary"] or "")
        self.assertNotIn("相对偏高", syn["uncertainty_note"] or "")
        # 程序化摘要是精确事实（1449 语义验收反例的 server 替代呈现）
        self.assertIn("-1.9%", syn["summary"])
        self.assertIn("8/10", syn["summary"])
        self.assertIn("22.2/100", syn["summary"])
        # claim 单独立示（模型 origin 可见，不冒充 server 摘要）
        claim_texts = [c["text"] for c in syn["claims"]]
        self.assertIn(wrong, claim_texts)
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        archived = detail.json()["data"]["analysis_json"]
        if isinstance(archived, str):
            archived = json.loads(archived)
        self.assertNotIn(wrong, archived["sector_synthesis"]["summary"] or "")
        self.assertNotIn(wrong,
                         archived["sector_synthesis"]["uncertainty_note"] or "")
        md = detail.json()["data"]["markdown"]
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(md, body["markdown"])
        self.assertEqual(download.text, body["markdown"])

    def test_missing_peer_change_disclosed_not_whole_sample_direction(self):
        """root 反例（600519 +1.0 / 000568 None）：公开载荷保留成员与比较
        范围缺口，模型投影只披露观测缺失——不得向模型断言全样本方向一致。"""
        import json as _json

        claims = [{"kind": "risk",
                   "text": "同业样本当日涨跌观测不完整，全样本方向未知，"
                           "政策催化的实际影响存在不确定性，短期情绪承压"
                           "且持续性有待验证",
                   "evidence_refs": ["intraday", "peers", "cat-1"]}]
        cons = pd.DataFrame({
            "代码": ["600519", "000568"],
            "名称": ["股票600519", "股票000568"],
            "涨跌幅": [1.0, None],   # 一家缺当日涨跌观测（成员保留）
            "成交额": [2.0e8, 1.0e8],
        })
        body, captured = self._post_weak_snapshot(claims, cons_df=cons)
        # 公开载荷：成员保留 + 有效比较范围缺口如实（不缩分母）
        peers = body["analysis"]["sector_peers"]
        self.assertEqual(len(peers["items"]), 2)
        self.assertTrue(any(i["change_pct"] is None for i in peers["items"]))
        self.assertTrue(any("有效比较范围 1/2" in g
                            for g in peers["gaps"]), peers["gaps"])
        # wire：peers 观察只披露缺失，无全样本方向结论
        wire_body = _json.loads(captured[-1].decode("utf-8"))
        user_text = wire_body["messages"][-1]["content"]
        self.assertIn("观测缺失", user_text)
        self.assertIn("无法得出全样本方向结论", user_text)
        self.assertNotIn("方向一致", user_text)
        self.assertNotIn("方向存在差异", user_text)
        # strict 存档/读回与三口一致照常
        self.assertEqual(body["analysis"]["sector_synthesis"]["status"], "ok")
        rid = body["report_id"]
        detail = self.client.get(f"/api/v1/sector-analysis/reports/{rid}")
        self.assertEqual(detail.status_code, 200)
        download = self.client.get(
            f"/api/v1/sector-analysis/reports/{rid}/markdown?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(detail.json()["data"]["markdown"], body["markdown"])
        self.assertEqual(download.text, body["markdown"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
