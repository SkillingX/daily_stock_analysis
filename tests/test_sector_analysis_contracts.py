# -*- coding: utf-8 -*-
"""板块分析模块 icontract 契约测试 (Layer 2 三层防御)。

注意：契约测试必须开 ICONTRACT_SLOW=true 跑（CI 已在 type-safety.yml 配置）：
- 违反契约时 icontract.ViolationError 应被抛出
- 满足契约时正常返回
- 离线运行，不依赖网络

契约来源：src/services/sector_analysis_service.py 中的 @require / @ensure 装饰器
- _prosperity_pillar（当日行情支柱）：4 维 None 或 [0, 100]；rank_of ∈ [1, total_boards]；
  budget 必须是未到期的 ReportBudget
- compose_report（v2 编排）：code 6 位数字；返回 v2 载荷契约键；
  assessment.status 与 identity.subject_status 一致；载荷不携带旧综合评分字段
"""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd
import pytest
from icontract import ViolationError

from src.services import sector_analysis_service as svc
from src.services.sector_analysis_service import BudgetExhausted, ReportBudget
from src.services.sector_sw_market import qualify_quote_rows


def _budget(seconds: float = 30.0) -> ReportBudget:
    return ReportBudget(seconds)


# ========================================================================
# _prosperity_pillar 契约（当日行情支柱；mock 网络层后离线验证）
# ========================================================================

def _em_table(rows: int = 86) -> pd.DataFrame:
    df = pd.DataFrame({
        "板块名称": [f"板块{i}" for i in range(rows)],
        "板块代码": [f"BK{i:04d}" for i in range(rows)],
        "涨跌幅": [-(i + 1) * 0.1 for i in range(rows)],
        "换手率": [3.0] * rows,
        "成交额": [1e9] * rows,
        "总市值": [None] * rows,
        "上涨家数": [10] * rows,
        "下跌家数": [20] * rows,
        "领涨股票": ["x"] * rows,
        "领涨股票-涨跌幅": [None] * rows,
    })
    df.loc[36, "板块名称"] = "半导体"
    df.loc[36, "板块代码"] = "BK0036"
    df.loc[36, "涨跌幅"] = -3.7  # 第 37 高
    return df


class TestProsperityPillarContracts:
    """_prosperity_pillar 当日行情合约（rank_of ∈ [1, total_boards] + 4 维 [0, 100]）。"""

    def _run(self, monkeypatch: pytest.MonkeyPatch, table: pd.DataFrame,
             name: str = "半导体", board_code: str | None = None) -> Dict[str, Any]:
        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: table)
        return svc._prosperity_pillar(name, budget=_budget(), board_code=board_code)

    def test_rank_within_range_for_normal_output(self, monkeypatch):
        """正常计算路径的 rank_of 恒在 [1, total_boards]（分位数学保证）。

        命名修正（第二轮审查）：原名 test_rank_out_of_range_raises_contract
        实际只验证正常输出，并未触发契约违反——rank = total - below 数学上
        不可能越界，icontract ensure 是防御层。真正的越界反例由 Pydantic
        IntradayPillar.validate_rank 覆盖（见 TestIntradayRankSchemaDefense）。
        """
        import akshare as ak
        import pandas as pd

        n = 5
        df = pd.DataFrame({
            "板块名称": ["A", "半导体", "C", "D", "E"],
            "板块代码": [f"BK{i:04d}" for i in range(n)],
            "涨跌幅": [1.0, 2.0, 3.0, 4.0, 5.0],
            "换手率": [None] * n,
            "成交额": [None] * n,
            "上涨家数": [None] * n,
            "下跌家数": [None] * n,
        })
        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: df)
        out = svc._prosperity_pillar("半导体", budget=_budget())
        assert out["sector_found"] is True
        assert out["rank_of"] is not None
        assert 1 <= out["rank_of"] <= out["total_boards"]

    def test_rank_within_total_boards(self, monkeypatch):
        result = self._run(monkeypatch, _em_table())
        assert result["sector_found"] is True
        assert result["total_boards"] == 86
        assert result["rank_of"] is not None
        assert 1 <= result["rank_of"] <= result["total_boards"]

    def test_dims_none_or_in_range(self, monkeypatch):
        result = self._run(monkeypatch, _em_table())
        for dim in ("momentum", "activity", "amount_activity", "breadth"):
            assert result[dim] is None or 0.0 <= result[dim] <= 100.0

    def test_expired_budget_raises_contract(self, monkeypatch):
        """到期 budget 违反 require 契约。"""
        import time

        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: _em_table())
        budget = ReportBudget(0.05)
        time.sleep(0.1)
        with pytest.raises((ViolationError, BudgetExhausted)):
            svc._prosperity_pillar("半导体", budget=budget)

    def test_non_string_name_raises_contract(self, monkeypatch):
        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: _em_table())
        with pytest.raises(ViolationError):
            svc._prosperity_pillar(123, budget=_budget())  # type: ignore[arg-type]


class TestIntradayRankSchemaDefense:
    """rank 越界防御的真实反例：载荷层 Pydantic model_validator 拒绝越界。

    正常计算路径（rank = total - below）数学上不可能越界，icontract ensure
    是防御层；schema 的 model_validator 是越界到达载荷时的实际守门。
    """

    def test_rank_above_total_boards_rejected(self):
        from pydantic import ValidationError

        from src.schemas.sector_analysis import IntradayPillar

        with pytest.raises(ValidationError, match="board population"):
            IntradayPillar(rank_of=999, total_boards=86)

    def test_rank_within_total_boards_accepted(self):
        from src.schemas.sector_analysis import IntradayPillar

        pillar = IntradayPillar(rank_of=86, total_boards=86)
        assert pillar.rank_of == 86


# ========================================================================
# compose_report 契约（v2 编排）
# ========================================================================

class TestComposeReportContracts:
    """compose_report v2 编排合约。"""

    @pytest.fixture(autouse=True)
    def _mock_pillars(self, monkeypatch):
        monkeypatch.setattr(
            svc, "_identify_sector",
            lambda code, *, budget: {
                "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
                "board_name": "白酒Ⅱ", "board_code": "BK1277",
                "subject_status": "confirmed",
                "membership": {"verified": True, "member_count": 20},
                "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
                "classification_system": "东财行业板块",
                "classification_level": None, "classification_version": None,
                "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
                "sector_name": "白酒Ⅱ", "gaps": [], "_board_table": None,
            },
        )
        monkeypatch.setattr(
            svc, "_policy_pillar",
            lambda text: {"lean": "supportive", "label": "支持", "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_base_rate_pillar",
            lambda text: {
                "base_rate": 0.5, "basis": "test", "hit_keyword": "白酒",
                "is_default": False, "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": 0.5, "all_rates": [],
            },
        )
        monkeypatch.setattr(
            svc, "_prosperity_pillar",
            lambda name, *, budget, board_code=None, board_table=None: {
                "sector_found": True,
                "momentum": 80.0, "activity": 60.0, "amount_activity": 40.0, "breadth": 100.0,
                "rank_of": 1, "total_boards": 86,
                "row": {}, "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_concepts_pillar",
            lambda code, *, budget: {"items": [], "source": "ifind",
                                     "total_returned": 0, "truncated": False,
                                     "gaps": []},
        )
        # issue #82：趋势/同业支柱与申万链为 compose 直接依赖，一并替身
        monkeypatch.setattr(
            svc, "_sector_trend_pillar",
            lambda code, name, *, budget, subject_confirmed=False, current_time=None: {
                "eligible": bool(subject_confirmed),
                "window_completed_sessions": 20, "interval_count": 19,
                "start_date": "2026-10-09" if subject_confirmed else None,
                "end_date": "2026-11-04" if subject_confirmed else None,
                "expected_dates": ["2026-10-09", "2026-10-12"] if subject_confirmed else [],
                "period_return_pct": 5.1234 if subject_confirmed else None,
                "benchmark": ({
                    "name": "沪深300", "symbol": "sh000300", "source": "东财指数日线",
                    "start_date": "2026-10-09", "end_date": "2026-11-04",
                    "qualified_sessions": 20, "period_return_pct": 3.8889, "gaps": [],
                } if subject_confirmed else None),
                "relative_return_pp": 1.2345 if subject_confirmed else None,
                "source": "东财行业板块日线" if subject_confirmed else None,
                "observed_at": "2026-11-05T14:00:02", "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_peers_pillar",
            lambda code, cons, *, budget, snapshot_observed_at=None,
            subject_confirmed=False, **_kwargs: {
                "eligible": bool(subject_confirmed),
                "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
                "items": ([
                    {"code": code, "name": "测试股票", "amount": 3.1e9,
                     "change_pct": 1.65, "is_target": True},
                ] if subject_confirmed else []),
                "target_in_amount_top5": True if subject_confirmed else None,
                "target_change_pct_rank": 1 if subject_confirmed else None,
                "sample_size": 1 if subject_confirmed else 0,
                "snapshot_observed_at": "2026-11-05T14:00:01" if subject_confirmed else None,
                "source": "东财行业板块成分（身份验证同一快照）" if subject_confirmed else None,
                "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_resolve_sw_chain",
            lambda code, text, *, budget: {
                "l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                "l3_code": "851251.SI", "gaps": []},
        )

    def test_returns_v2_payload_contract_keys(self):
        result = svc.compose_report("600519", "测试股票", budget=_budget())
        required = {"payload_version", "stock_code", "stock_name", "generated_at",
                    "identity", "background", "concepts", "assessment", "gaps"}
        assert required.issubset(result.keys())
        assert result["payload_version"] == 2

    def test_status_agrees_with_subject(self):
        result = svc.compose_report("600519", "测试股票", budget=_budget())
        assert result["assessment"]["status"] == "unscored"
        assert result["identity"]["subject_status"] == "confirmed"

    def test_retired_composite_fields_stay_none(self):
        """评分退役字段保留路径但显式 None（追加兼容 + 不恢复旧结论）。"""
        result = svc.compose_report("600519", "测试股票", budget=_budget())
        for retired in ("sector_score", "band", "beta_meaning", "weights",
                        "prosperity_score"):
            assert result[retired] is None
        # 非评分事实路径投影可读
        assert result["identification"]["sector_name"] == "白酒Ⅱ"
        assert result["policy"]["score"] is None
        assert result["prosperity"]["capital"] is None

    def test_invalid_code_raises_contract(self):
        with pytest.raises(ViolationError):
            svc.compose_report("12345", "测试", budget=_budget())  # 5 位

    def test_non_digit_code_raises_contract(self):
        with pytest.raises(ViolationError):
            svc.compose_report("ABC123", "测试", budget=_budget())  # 含字母

    def test_too_long_code_raises_contract(self):
        with pytest.raises(ViolationError):
            svc.compose_report("1234567", "测试", budget=_budget())  # 7 位

    def test_inconsistent_status_raises_contract(self, monkeypatch):
        """身份 confirmed 但 assessment 被改为 insufficient_data：跨字段不变式守门。"""
        import src.services.sector_analysis_service as mod

        orig = mod._compose_status
        monkeypatch.setattr(mod, "_compose_status", lambda ident: ("insufficient_data", ["x"]))
        try:
            with pytest.raises(ViolationError):
                svc.compose_report("600519", "测试股票", budget=_budget())
        finally:
            monkeypatch.setattr(mod, "_compose_status", orig)


# ========================================================================
# issue #82 新契约：区间收益（Decimal）+ 趋势/同业资格一致性
# ========================================================================


class TestPeriodReturnContracts:
    """_period_return_pct：入参 >=2 个正有限 Decimal，出参有限 Decimal。"""

    def test_valid_input_returns_finite_decimal(self):
        from decimal import Decimal

        out = svc._period_return_pct([Decimal("100"), Decimal("110")])
        assert isinstance(out, Decimal) and out.is_finite()
        assert out == Decimal("10.0")

    def test_single_close_raises(self):
        from decimal import Decimal

        with pytest.raises(ViolationError):
            svc._period_return_pct([Decimal("100")])

    def test_zero_or_negative_close_raises(self):
        from decimal import Decimal

        with pytest.raises(ViolationError):
            svc._period_return_pct([Decimal("100"), Decimal("0")])
        with pytest.raises(ViolationError):
            svc._period_return_pct([Decimal("100"), Decimal("-1")])


class TestTrendPeersEligibilityContracts:
    """compose_report：trend/peers eligible 必须严格等于 confirmed 资格。"""

    @pytest.fixture(autouse=True)
    def _zero_tcp_guard(self, monkeypatch: pytest.MonkeyPatch):
        """零出站 TCP 守卫：任何 connect 尝试立即失败并计数，用例结束要求 0 次。

        不满足于「出站异常被服务层吞成 partial success」：只要发生连接
        尝试，计数即 > 0，用例以失败告终。即使进程 config 启用 iFinD，
        本套件也已替身非目标支柱（_prosperity_pillar/_concepts_pillar），
        不会读取真实凭据或发起任何供应商请求。
        """
        import socket

        attempts: list[str] = []

        def _guarded_connect(sock: socket.socket, address: Any) -> None:
            attempts.append(repr(address))
            raise AssertionError(f"unexpected outbound TCP attempt: {address!r}")

        monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
        yield
        assert not attempts, (
            f"expected ZERO outbound TCP attempts, got {len(attempts)}: {attempts}"
        )

    def _run(self, monkeypatch: pytest.MonkeyPatch, *, subject: str,
             trend_eligible: bool, peers_eligible: bool):
        monkeypatch.setattr(
            svc, "_identify_sector",
            lambda code, *, budget: {
                "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
                "board_name": "白酒Ⅱ", "board_code": "BK1277",
                "subject_status": subject,
                "membership": {"verified": subject == "confirmed",
                               "member_count": 20},
                "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
                "classification_system": "东财行业板块",
                "classification_level": None, "classification_version": None,
                "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
                "sector_name": "白酒Ⅱ", "gaps": [], "_board_table": None,
            },
        )
        monkeypatch.setattr(
            svc, "_sector_trend_pillar",
            lambda code, name, *, budget, subject_confirmed=False, current_time=None: {
                "eligible": trend_eligible, "window_completed_sessions": 20,
                "interval_count": 19, "start_date": None, "end_date": None,
                "expected_dates": [], "period_return_pct": None, "benchmark": None,
                "relative_return_pp": None, "source": None,
                "observed_at": "2026-11-05T14:00:02", "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_peers_pillar",
            lambda code, cons, *, budget, snapshot_observed_at=None,
            subject_confirmed=False, **_kwargs: {
                "eligible": peers_eligible,
                "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
                "items": [], "target_in_amount_top5": None,
                "target_change_pct_rank": None, "sample_size": 0,
                "snapshot_observed_at": None, "source": None, "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_resolve_sw_chain",
            lambda code, text, *, budget: {
                "l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
        )
        # 非目标支柱替身（本套件只测 compose_report 资格一致性 @ensure）：
        # compose 拿到 _board_table=None 会触发 _prosperity_pillar 真实出站
        # （AkShare 板块表），_concepts_pillar 在 config 启用 iFinD 时出站
        # （MCP 供应商）。二者是本套件唯一的真实供应商边界，替身后完全离线；
        # policy/base_rate 是纯本地计算，不属于供应商边界，保持真实路径。
        monkeypatch.setattr(
            svc, "_prosperity_pillar",
            lambda name, *, budget, board_code=None, board_table=None: {
                "sector_found": True,
                "momentum": 80.0, "activity": 60.0, "amount_activity": 40.0,
                "breadth": 100.0, "rank_of": 1, "total_boards": 86,
                "row": {}, "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_concepts_pillar",
            lambda code, *, budget: {"items": [], "source": "ifind",
                                     "total_returned": 0, "truncated": False,
                                     "gaps": []},
        )
        # 生产编排缝（拆分后唯一入口）：核心事实缝 + 完整研究组装
        # （_compose_eastmoney_report 转发入口已随 #102 复审移除，
        # 其四条后置契约由 _compose_eastmoney_report_from_core 唯一持有）
        core = svc._compose_eastmoney_core("600519", budget=_budget())
        return svc._compose_eastmoney_report_from_core(
            core, "600519", "测试股票", budget=_budget())

    def test_confirmed_with_matching_eligibility_passes(self, monkeypatch):
        result = self._run(monkeypatch, subject="confirmed",
                           trend_eligible=True, peers_eligible=True)
        assert result["sector_trend"]["eligible"] is True
        assert result["sector_peers"]["eligible"] is True

    def test_unconfirmed_with_matching_eligibility_passes(self, monkeypatch):
        result = self._run(monkeypatch, subject="unverified",
                           trend_eligible=False, peers_eligible=False)
        assert result["sector_trend"]["eligible"] is False

    def test_confirmed_but_trend_not_eligible_raises(self, monkeypatch):
        """confirmed 身份但趋势块未取得资格（实现错位）：契约拒绝。"""
        with pytest.raises(ViolationError):
            self._run(monkeypatch, subject="confirmed",
                      trend_eligible=False, peers_eligible=True)

    def test_unconfirmed_but_peers_eligible_raises(self, monkeypatch):
        """unverified 身份但同业块给了资格（无资格查询）：契约拒绝。"""
        with pytest.raises(ViolationError):
            self._run(monkeypatch, subject="unverified",
                      trend_eligible=False, peers_eligible=True)


# ========================================================================
# trends2 金融派生契约（Layer 2；root 收敛轮新增公式边界）
# ========================================================================

class TestTrendsFinancialDerivationContracts:
    """trends2 累计额与涨跌幅 typed 金融公式（icontract require/ensure）。

    反例来自 root 审查：parsed 位置魔数曾把最高价当现价——preClose=10、
    close=11、high=15 必须得 10.00%（非 50%）；累计额必须来自完整合格
    分钟额（部分和不冒充当日累计）。
    """

    def test_change_pct_close_column_ten_percent(self):
        assert svc._trends_change_pct(
            __import__("decimal").Decimal("11"),
            __import__("decimal").Decimal("10"),
        ) == __import__("decimal").Decimal("10.00")

    def test_change_pct_independent_real_wire_values(self):
        from decimal import Decimal

        # 真实 wire 独立对照：600519 末分钟 close=1453 / preClose=1452
        assert svc._trends_change_pct(
            Decimal("1453"), Decimal("1452")) == Decimal("0.07")
        # BK1277 末分钟 close=4543.15 / preClose=4642.21（close≠high 列判别）
        assert svc._trends_change_pct(
            Decimal("4543.15"), Decimal("4642.21")) == Decimal("-2.13")

    def test_change_pct_high_column_not_used(self):
        from decimal import Decimal

        # 若实现误用 high=15：(15-10)/10*100=50% —— 公式只接受显式现价入参，
        # 输入域由 require 限定，调用方（wire 解析）负责取 close 列
        result = svc._trends_change_pct(Decimal("11"), Decimal("10"))
        assert result != Decimal("50.00")

    def test_change_pct_zero_preclose_raises(self):
        from decimal import Decimal

        with pytest.raises(ViolationError):
            svc._trends_change_pct(Decimal("11"), Decimal("0"))

    def test_change_pct_negative_preclose_raises(self):
        from decimal import Decimal

        with pytest.raises(ViolationError):
            svc._trends_change_pct(Decimal("11"), Decimal("-10"))

    def test_change_pct_non_finite_raises(self):
        from decimal import Decimal

        with pytest.raises(ViolationError):
            svc._trends_change_pct(Decimal("Infinity"), Decimal("10"))

    def test_sum_minute_amounts_golden_independent(self):
        from decimal import Decimal

        # 真实 wire 前两分钟额（600519 09:30/09:31）逐值可核算；
        # 全 241 行金标准求和由 wire 全链测试覆盖（6199528292）
        assert svc._sum_minute_amounts(
            [Decimal("226345000.00"), Decimal("420208080.00")],
        ) == Decimal("646553080.00")

    def test_sum_minute_amounts_partial_zero_minutes_legal(self):
        from decimal import Decimal

        # 零额分钟是合法观测：求和只累计已通过行级资格的有限非负额
        assert svc._sum_minute_amounts(
            [Decimal("0"), Decimal("100.5")]) == Decimal("100.5")

    def test_sum_minute_amounts_negative_raises(self):
        from decimal import Decimal

        with pytest.raises(ViolationError):
            svc._sum_minute_amounts([Decimal("-1")])

    def test_sum_minute_amounts_empty_raises(self):
        with pytest.raises(ViolationError):
            svc._sum_minute_amounts([])


# ========================================================================
# 腾讯公开行情批量金额/币种契约（Layer 2；共享字段约定，Decimal 无 float 换算）
# ========================================================================

class TestTencentAmountContracts:
    """既有优先级契约：字段35三元组第三段=精确金额（元）优先；字段37=万元
    兜底（仅精确金额缺值时 ×10000）；字段35 存在但畸形/负值/非有限 = 真实
    坏值，不回退兜底、不补 0 冒充（金额未知）。B 股代码币种可比性未证实。"""

    def _fields(self, exact: str, wan: str) -> list:
        fields = [""] * 38
        fields[35] = exact
        fields[37] = wan
        return fields

    def test_exact_triplet_amount_decimal(self):
        from decimal import Decimal

        amount, known, source = svc._parse_tencent_amount_decimal(
            self._fields("34.91/68243/239321750", "23932"))
        assert known is True and amount == Decimal("239321750")
        assert source == "exact35"

    def test_known_zero_exact_is_known_zero(self):
        from decimal import Decimal

        amount, known, source = svc._parse_tencent_amount_decimal(
            self._fields("0/0/0", "0"))
        assert known is True and amount == Decimal("0")
        assert source == "exact35"

    def test_wan_fallback_only_when_exact_absent(self):
        from decimal import Decimal

        amount, known, source = svc._parse_tencent_amount_decimal(
            self._fields("", "949"))
        # 字段37为万元整口（949×10000=9490000）；精确分位在字段35三元组
        assert known is True and amount == Decimal("9490000")
        assert source == "rounded37"

    def test_malformed_exact_does_not_fall_back(self):
        amount, known, source = svc._parse_tencent_amount_decimal(
            self._fields("12.34/abc", "949"))
        assert known is False and amount is None and source is None

    def test_negative_exact_rejected(self):
        amount, known, source = svc._parse_tencent_amount_decimal(
            self._fields("1/1/-5", "5"))
        assert known is False and amount is None and source is None

    def test_nonfinite_exact_rejected(self):
        amount, known, source = svc._parse_tencent_amount_decimal(
            self._fields("1/1/Infinity", "5"))
        assert known is False and amount is None and source is None

    def test_both_missing_amount_unknown(self):
        amount, known, source = svc._parse_tencent_amount_decimal(
            self._fields("", ""))
        assert known is False and amount is None and source is None

    def test_b_share_codes_uncertain_currency(self):
        assert svc._is_uncertain_currency_code("200596") is True
        assert svc._is_uncertain_currency_code("900901") is True
        assert svc._is_uncertain_currency_code("600519") is False
        assert svc._is_uncertain_currency_code("920001") is False  # BJ 合法 CNY


class TestSwPeersPillarContracts:
    """成分边界不变式（issue #100）：行业参考态下目标股不进成分样本与名次。"""

    def _constituents(self, status: str) -> Dict[str, Any]:
        codes = ["000552.SZ", "600188.SH", "601225.SH", "601898.SH"]
        return {
            "membership": {"status": status, "target_secucode":
                           ("600188.SH" if status == "included" else "920101.BJ"),
                           "observation_date": "2026-11-18",
                           "member_count": len(codes),
                           "completeness_evidence": None, "gaps": []},
            "members": {"member_codes": codes,
                        "member_names": {c: f"成员{c[:6]}" for c in codes},
                        "dates_seen": ["2026-11-18"], "rows_total": len(codes),
                        "completeness_evidence": None, "gaps": []},
            "member_quotes": {c: {"thscode": c, "trade_date": "2026-11-18",
                                  "trade_time": "22:00:46", "change_pct": 1.0,
                                  "amount": 5.0e8} for c in codes},
            "member_quote_gaps": [],
            "target_quote": (None if status == "included" else
                             {"thscode": "920101.BJ",
                              "trade_date": "2026-11-18",
                              "trade_time": "22:00:46", "change_pct": 1.5,
                              "amount": 4.0e7}),
            "target_quote_gaps": [],
            "target_name": "测试标的",
        }

    def test_included_target_inside_sample_satisfies_contracts(self):
        out = svc._sw_peers_pillar(
            "600188", constituents=self._constituents("included"),
            snapshot_observed_at="2026-11-18T23:00:00",
            budget=ReportBudget(60.0, finish_margin=5.0))
        assert any(i["is_target"] for i in out["items"])
        assert out["industry_reference"] is False
        assert out["target_independent_quote"] is None

    def test_reference_boundary_target_absent_from_items_and_ranks(self):
        out = svc._sw_peers_pillar(
            "920101", constituents=self._constituents("unknown"),
            snapshot_observed_at="2026-11-18T23:00:00",
            budget=ReportBudget(60.0, finish_margin=5.0))
        assert out["industry_reference"] is True
        assert out["target_change_pct_rank"] is None
        assert out["target_in_amount_top5"] is None
        assert not any(i["is_target"] for i in out["items"])
        assert out["target_independent_quote"]["code"] == "920101"
        assert not any(i["code"] == "920101" for i in out["items"])

    def test_boundary_contracts_attached(self):
        """行业参考态不变式以 icontract 契约强制（每次调用自动校验）。"""
        postconditions = getattr(svc._sw_peers_pillar, "__postconditions__", [])
        assert postconditions, "_sw_peers_pillar 应挂载 ensure 契约"
        preconditions = getattr(svc._sw_peers_pillar, "__preconditions__", [])
        assert preconditions, "_sw_peers_pillar 应挂载 require 契约"


# ========================================================================
# qualify_quote_rows 契约（报价资格边界；#102 复审 Standards 1）
# ========================================================================

class TestQuoteQualificationContracts:
    """快照行资格出口不变式（icontract @ensure）：带涨跌幅且带复核基准的
    qualified 行必须与 latest/preClose 的 Decimal 推算涨跌幅落在既有
    0.005pp 容差内；金额口径行（无涨跌幅）与无复核基准行不发明断言。"""

    @staticmethod
    def _row(code: str, change: float, latest: float,
             pre_close: float) -> Dict[str, Any]:
        from datetime import datetime as _dt

        return {"thscode": code, "trade_date": "2026-11-18",
                "trade_time": "22:00:46",
                "stamp": _dt(2026, 11, 18, 22, 0, 46),
                "change_pct": change, "latest": latest,
                "pre_close": pre_close, "amount": 1.0e9}

    def test_boundary_counterexample_is_rejected_not_adopted(self):
        """受控反例（latest=100.5 / preClose=100 / changeRatio=0.495，确切
        差值 0.005pp）：按 Decimal 落在容差边界 → 行被剔除（二进制浮点曾
        算成 0.00499… 并错误采纳）。"""
        rows = [self._row("801950.SL", 0.495, 100.5, 100.0)]
        qualified, _notes = qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt_factory(2026, 11, 19, 1, 35))
        assert qualified == {}

    def test_interior_examples_pass_and_carry_qualified_change(self):
        """容差内侧反例照常采纳，出口契约随行成立（每次调用自动校验）。"""
        rows = [self._row("801950.SL", 0.496, 100.5, 100.0),
                self._row("801120.SL", -0.867, 2520.109, 2542.15)]
        qualified, _notes = qualify_quote_rows(
            rows, session_date="2026-11-18",
            received_at=_dt_factory(2026, 11, 19, 1, 35))
        assert set(qualified) == {"801950.SL", "801120.SL"}

    def test_member_amount_only_row_satisfies_contract_without_change(self):
        """成分金额口径（require_change=False）：无涨跌幅行合格且不触发出
        口契约（谓词对无涨跌幅行不发明断言）。"""
        row = self._row("600188.SH", 0.0, 100.0, 100.0)
        row["change_pct"] = None
        qualified, _notes = qualify_quote_rows(
            [row], session_date="2026-11-18",
            received_at=_dt_factory(2026, 11, 19, 1, 35),
            require_change=False)
        assert set(qualified) == {"600188.SH"}
        assert qualified["600188.SH"]["change_pct"] is None

    def test_contract_attached_to_qualification_boundary(self):
        """交叉复核不变式以 icontract 契约强制在资格边界（出口校验）。"""
        postconditions = getattr(qualify_quote_rows, "__postconditions__",
                                 [])
        assert postconditions, "qualify_quote_rows 应挂载 ensure 契约"


def _dt_factory(*args: int) -> Any:
    from datetime import datetime as _dt

    return _dt(*args)


# ========================================================================
# 多窗口序列行契约（issue #103 复审；icontract 出口校验）
# ========================================================================


class TestTrendWindowSeriesContracts:
    """多窗口序列行跨字段不变式（挂在实际构建路径 ``_sw_trend_windows_pillar``
    出口）：合格日数与观察明细一致（``qualified_sessions == len(observations)``）、
    已发布收益对应完整对齐窗口（收益非 None ⇒ 明细覆盖全部期望交易日）。
    schema 反例（expected=6/qualified=2/observations=[]/return=1）必须被拒。
    """

    @staticmethod
    def _full_frames(sessions):
        import pandas as pd

        def _frame(code):
            return pd.DataFrame({
                "date": [d.isoformat() for d in sessions],
                "close": [100.0 + i for i in range(len(sessions))],
            })

        return {code: _frame(code) for code in
                ("801194.SL", "801790.SL", "000300.SH")}

    def _run_pillar(self, monkeypatch, sessions, frames):
        from src.services import sector_sw_market as sw_mod

        monkeypatch.setattr(svc, "recent_completed_trading_sessions",
                            lambda market, n, current_time=None: sessions[:n])
        monkeypatch.setattr(sw_mod, "fetch_index_history",
                            lambda *a, **k: frames)
        return svc._sw_trend_windows_pillar(
            "801194.SL", "保险Ⅱ", "801790.SL", "非银金融",
            budget=ReportBudget(60.0, finish_margin=5.0),
            refresh_token="t", timeout_seconds=5.0)

    def test_valid_output_satisfies_contracts(self, monkeypatch):
        from datetime import date, timedelta

        sessions = []
        d = date(2026, 8, 3)
        while len(sessions) < 61:
            if d.weekday() < 5:
                sessions.append(d)
            d += timedelta(days=1)
        out = self._run_pillar(monkeypatch, sessions,
                               self._full_frames(sessions))
        assert svc._trend_window_series_invariants(out)
        w5 = out["windows"][0]
        for s in w5["series"]:
            assert s["qualified_sessions"] == len(s["observations"]) == 6
            assert s["period_return_pct"] is not None

    def test_observations_missing_violates_contract(self, monkeypatch):
        """缝反例 A：合格日数与观察明细脱节（明细被清空）→ ViolationError。"""
        from datetime import date, timedelta

        sessions = []
        d = date(2026, 8, 3)
        while len(sessions) < 61:
            if d.weekday() < 5:
                sessions.append(d)
            d += timedelta(days=1)
        orig = svc._observation_rows
        monkeypatch.setattr(svc, "_observation_rows", lambda obs: [])
        with pytest.raises(ViolationError):
            self._run_pillar(monkeypatch, sessions,
                             self._full_frames(sessions))
        monkeypatch.setattr(svc, "_observation_rows", orig)

    def test_published_return_requires_full_window_rows(self, monkeypatch):
        """缝反例 B：收益已发布但观察明细不完整（截断为一行）→
        ViolationError（收益只许伴随完整对齐窗口发布）。"""
        from datetime import date, timedelta

        sessions = []
        d = date(2026, 8, 3)
        while len(sessions) < 61:
            if d.weekday() < 5:
                sessions.append(d)
            d += timedelta(days=1)
        orig = svc._observation_rows
        monkeypatch.setattr(
            svc, "_observation_rows",
            lambda obs: orig(dict(list(obs.items())[:1])))
        with pytest.raises(ViolationError):
            self._run_pillar(monkeypatch, sessions,
                             self._full_frames(sessions))
        monkeypatch.setattr(svc, "_observation_rows", orig)

    def test_predicate_rejects_schema_counterexample_shape(self):
        """谓词反例（schema 可接受而契约拒绝的形状）：expected=6、
        qualified=2、observations=[]、period_return_pct=1 必须判 False。"""
        bad = {"windows": [{"series": [{
            "expected_sessions": 6, "qualified_sessions": 2,
            "observations": [], "period_return_pct": 1.0}]}]}
        assert svc._trend_window_series_invariants(bad) is False
        # 部分合格且收益留空的合法形状不误伤
        partial = {"windows": [{
            "expected_dates": ["2026-11-13", "2026-11-16", "2026-11-17",
                               "2026-11-18", "2026-11-19", "2026-11-20"],
            "series": [{
                "expected_sessions": 6, "qualified_sessions": 2,
                "observations": [{"date": "2026-11-13", "close": 100.0},
                                 {"date": "2026-11-17", "close": 101.0}],
                "period_return_pct": None}]}]}
        assert svc._trend_window_series_invariants(partial) is True

    def test_published_return_rejects_equal_count_repeated_dates(self):
        """issue #105 修复反例 A（当前 PR 版本可接受、修复后拒绝）：5 日窗
        同一交易日重复六次且携带收益——数量与点数对齐但观测日期未按序
        等于本窗完整预期日期，收益发布必须拒绝。"""
        bad = {"windows": [{
            "point_count": 6,
            "expected_dates": ["2026-11-13", "2026-11-16", "2026-11-17",
                               "2026-11-18", "2026-11-19", "2026-11-20"],
            "series": [{
                "expected_sessions": 6, "qualified_sessions": 6,
                "observations": [{"date": "2026-11-18", "close": 100.0},
                                 {"date": "2026-11-18", "close": 101.0},
                                 {"date": "2026-11-18", "close": 102.0},
                                 {"date": "2026-11-18", "close": 103.0},
                                 {"date": "2026-11-18", "close": 104.0},
                                 {"date": "2026-11-18", "close": 105.0}],
                "period_return_pct": 5.0}]}]}
        assert svc._trend_window_series_invariants(bad) is False

    def test_both_sides_repeating_same_date_rejected_at_window_level(self):
        """issue #105 修复二轮反例：预期日期与观察**双方**重复同一日期——
        旧的逐位相等比较会放行（两份列表逐字相同），新契约在窗口级先拒
        绝（预期日期必须时间有序且唯一），无论是否携带收益。"""
        repeated = {"windows": [{
            "point_count": 6,
            "expected_dates": ["2026-11-18"] * 6,
            "series": [{
                "expected_sessions": 6, "qualified_sessions": 6,
                "observations": [{"date": "2026-11-18", "close": 100.0 + i}
                                 for i in range(6)],
                "period_return_pct": 5.0}]}]}
        assert svc._trend_window_series_invariants(repeated) is False
        repeated["windows"][0]["series"][0]["period_return_pct"] = None
        assert svc._trend_window_series_invariants(repeated) is False

    def test_out_of_window_partial_observations_rejected_without_return(self):
        """issue #105 修复二轮反例：无收益的部分观测含窗外日期（旧版契约
        只在有收益时查对齐，窗外观察曾可发布）——观察必须是预期日期的
        有序子集，收益留空同样拒绝。"""
        out_of_window = {"windows": [{
            "point_count": 6,
            "expected_dates": ["2026-11-13", "2026-11-16", "2026-11-17",
                               "2026-11-18", "2026-11-19", "2026-11-20"],
            "series": [{
                "expected_sessions": 6, "qualified_sessions": 2,
                "observations": [{"date": "2026-11-12", "close": 100.0},
                                 {"date": "2026-11-13", "close": 101.0}],
                "period_return_pct": None}]}]}
        assert svc._trend_window_series_invariants(out_of_window) is False

    def test_legal_partial_and_calendar_shortfall_allowed(self):
        """issue #105 修复二轮正例：真正合规的部分观测（观察 = 预期日期的
        有序子集、无收益）与日历不足（expected 少于规定点数）都允许。"""
        partial = {"windows": [{
            "point_count": 6,
            "expected_dates": ["2026-11-13", "2026-11-16", "2026-11-17",
                               "2026-11-18", "2026-11-19", "2026-11-20"],
            "series": [{
                "expected_sessions": 6, "qualified_sessions": 2,
                "observations": [{"date": "2026-11-16", "close": 100.0},
                                 {"date": "2026-11-19", "close": 101.0}],
                "period_return_pct": None}]}]}
        assert svc._trend_window_series_invariants(partial) is True
        # 日历不足：窗口期望仅 3/6 个交易日（严格日历已完成交易日不够），
        # 部分观测仍是其有序子集
        shortfall = {"windows": [{
            "point_count": 6,
            "expected_dates": ["2026-11-18", "2026-11-19", "2026-11-20"],
            "series": [{
                "expected_sessions": 3, "qualified_sessions": 2,
                "observations": [{"date": "2026-11-18", "close": 100.0},
                                 {"date": "2026-11-20", "close": 101.0}],
                "period_return_pct": None}]}]}
        assert svc._trend_window_series_invariants(shortfall) is True

    def test_published_return_rejects_equal_count_wrong_dates(self):
        """issue #105 修复反例 B：点数相同但日期错位（旧日凑数/偏移一日）
        且携带收益——同样拒绝；窗外首日（2026-11-12）在无收益时同样拒绝
        （观察须为预期日期的有序子集）。"""
        wrong_dates = {"windows": [{
            "point_count": 6,
            "expected_dates": ["2026-11-13", "2026-11-16", "2026-11-17",
                               "2026-11-18", "2026-11-19", "2026-11-20"],
            "series": [{
                "expected_sessions": 6, "qualified_sessions": 6,
                "observations": [{"date": "2026-11-12", "close": 100.0},
                                 {"date": "2026-11-13", "close": 101.0},
                                 {"date": "2026-11-16", "close": 102.0},
                                 {"date": "2026-11-17", "close": 103.0},
                                 {"date": "2026-11-18", "close": 104.0},
                                 {"date": "2026-11-19", "close": 105.0}],
                "period_return_pct": 5.0}]}]}
        assert svc._trend_window_series_invariants(wrong_dates) is False
        # 窗外观察（首日 2026-11-12 不在预期窗口内）无收益时同样拒绝
        wrong_dates["windows"][0]["series"][0]["period_return_pct"] = None
        assert svc._trend_window_series_invariants(wrong_dates) is False

    def test_published_return_rejects_wrong_point_count(self):
        """issue #105 修复反例 C：收益发布但点数不等于该窗口规定点数
        （expected_sessions ≠ point_count）→ 拒绝；完整同日对齐的正例
        通过。"""
        wrong_count = {"windows": [{
            "point_count": 6,
            "expected_dates": ["2026-11-16", "2026-11-17", "2026-11-18",
                               "2026-11-19", "2026-11-20", "2026-11-21"],
            "series": [{
                "expected_sessions": 5, "qualified_sessions": 5,
                "observations": [{"date": "2026-11-16", "close": 100.0},
                                 {"date": "2026-11-17", "close": 101.0},
                                 {"date": "2026-11-18", "close": 102.0},
                                 {"date": "2026-11-19", "close": 103.0},
                                 {"date": "2026-11-20", "close": 104.0}],
                "period_return_pct": 4.0}]}]}
        assert svc._trend_window_series_invariants(wrong_count) is False
        aligned = {"windows": [{
            "point_count": 6,
            "expected_dates": ["2026-11-13", "2026-11-16", "2026-11-17",
                               "2026-11-18", "2026-11-19", "2026-11-20"],
            "series": [{
                "expected_sessions": 6, "qualified_sessions": 6,
                "observations": [{"date": "2026-11-13", "close": 100.0},
                                 {"date": "2026-11-16", "close": 101.0},
                                 {"date": "2026-11-17", "close": 102.0},
                                 {"date": "2026-11-18", "close": 103.0},
                                 {"date": "2026-11-19", "close": 104.0},
                                 {"date": "2026-11-20", "close": 105.0}],
                "period_return_pct": 5.0}]}]}
        assert svc._trend_window_series_invariants(aligned) is True

    def test_contracts_attached_to_construction_path(self):
        """出口不变式以 icontract 契约强制在实际构建路径（每次调用校验）。"""
        postconditions = getattr(
            svc._sw_trend_windows_pillar, "__postconditions__", [])
        assert postconditions, "_sw_trend_windows_pillar 应挂载 ensure 契约"


# ========================================================================
# 申万同业块成交额名次跨字段契约（issue #104 复审；icontract 出口校验）
# ========================================================================


class TestSwAmountRankInvariants:
    """``_sw_peers_pillar`` 出口的成交额名次跨字段不变式：名次发布需要声明
    范围且有界分母（1 ≤ rank ≤ denominator ≤ 正额行数）；full_constituents
    范围必携带双接口核验证据；成分数复核（verified_constituent_num）独立于
    名次发布——样本级块允许持有独立复核信息，不在契约中禁止。"""

    @staticmethod
    def _included_constituents(*, evidence: Any) -> Dict[str, Any]:
        codes = ["601318.SH", "601319.SH", "601336.SH", "601601.SH",
                 "601628.SH"]
        quotes = {
            "601318.SH": {"thscode": "601318.SH",
                          "trade_date": "2026-11-20",
                          "trade_time": "22:00:46", "change_pct": -0.73,
                          "amount": 2.81e9},
            "601319.SH": {"thscode": "601319.SH",
                          "trade_date": "2026-11-20",
                          "trade_time": "22:00:46", "change_pct": -1.39,
                          "amount": 3.12e8},
            "601336.SH": {"thscode": "601336.SH",
                          "trade_date": "2026-11-20",
                          "trade_time": "22:00:46", "change_pct": -1.91,
                          "amount": 4.32e8},
            "601601.SH": {"thscode": "601601.SH",
                          "trade_date": "2026-11-20",
                          "trade_time": "22:00:46", "change_pct": -0.06,
                          "amount": 6.26e8},
            "601628.SH": {"thscode": "601628.SH",
                          "trade_date": "2026-11-20",
                          "trade_time": "22:00:46", "change_pct": -0.60,
                          "amount": 0.0},
        }
        return {
            "membership": {"status": "included",
                           "target_secucode": "601318.SH",
                           "observation_date": "2026-11-20",
                           "member_count": 5,
                           "completeness_evidence": evidence, "gaps": []},
            "members": {"member_codes": codes,
                        "member_names": {c: f"成员{c[:6]}" for c in codes},
                        "dates_seen": ["2026-11-20"],
                        "rows_total": len(codes),
                        "completeness_evidence": None, "gaps": []},
            "member_quotes": quotes, "member_quote_gaps": [],
            "target_quote": None, "target_quote_gaps": [],
            "target_name": "中国平安",
        }

    def test_real_paths_satisfy_invariants(self) -> None:
        """真实业务路径正例：全量发布（含零额成员）、一级口径出口全部满足
        不变式（每次调用自动校验）；样本范围形状经谓词正例覆盖。"""
        evidence = ("iFinD 双接口核验一致：p03473 同日名单 5 只 = "
                    "ths_constituent_num_index 计数 5（FDATE 20261120）")
        full = svc._sw_peers_pillar(
            "601318", constituents=self._included_constituents(evidence=evidence),
            snapshot_observed_at="2026-11-20T23:00:00",
            budget=ReportBudget(60.0, finish_margin=5.0),
            amount_rank_enabled=True)
        assert full["amount_rank_scope"] == "full_constituents"
        assert full["amount_rank_evidence"] == evidence
        assert svc._sw_amount_rank_invariants(full) is True
        l1 = svc._sw_peers_pillar(
            "601318", constituents=self._included_constituents(evidence=None),
            snapshot_observed_at="2026-11-20T23:00:00",
            budget=ReportBudget(60.0, finish_margin=5.0))
        assert l1["amount_rank_scope"] is None
        assert svc._sw_amount_rank_invariants(l1) is True
        assert svc._sw_amount_rank_invariants(
            {"target_amount_rank": 1, "amount_rank_denominator": 4,
             "amount_rank_scope": "qualified_sample",
             "rankable_rows": 4}) is True
        assert svc._sw_amount_rank_invariants(
            {"target_amount_rank": None, "amount_rank_denominator": None,
             "amount_rank_scope": None, "rankable_rows": 4}) is True

    def test_predicate_rejects_out_of_range_rank(self) -> None:
        """反例：名次越过分母（5 > 4）必须判 False。"""
        bad = {"target_amount_rank": 5, "amount_rank_denominator": 4,
               "amount_rank_scope": "full_constituents",
               "amount_rank_evidence": "e", "rankable_rows": 4}
        assert svc._sw_amount_rank_invariants(bad) is False

    def test_predicate_rejects_full_scope_without_evidence(self) -> None:
        """反例：全成分范围名次不携带双接口核验证据必须判 False。"""
        bad = {"target_amount_rank": 1, "amount_rank_denominator": 4,
               "amount_rank_scope": "full_constituents",
               "amount_rank_evidence": None, "rankable_rows": 4}
        assert svc._sw_amount_rank_invariants(bad) is False

    def test_predicate_rejects_denominator_beyond_rankable_rows(self) -> None:
        """反例：分母越过正额行数（样本/全量范围的公共上界）必须判 False。"""
        bad = {"target_amount_rank": None, "amount_rank_denominator": 10,
               "amount_rank_scope": None, "rankable_rows": 4}
        assert svc._sw_amount_rank_invariants(bad) is False
        bad_rank = {"target_amount_rank": 3, "amount_rank_denominator": 10,
                    "amount_rank_scope": "qualified_sample",
                    "rankable_rows": 4}
        assert svc._sw_amount_rank_invariants(bad_rank) is False

    def test_sample_scope_may_carry_independent_verification(self) -> None:
        """样本级块持有独立复核信息不被禁止（成分数复核与全量名次分别
        表达——复审问题 6 的契约面）。"""
        ok = {"target_amount_rank": 2, "amount_rank_denominator": 4,
              "amount_rank_scope": "qualified_sample",
              "amount_rank_evidence": None, "rankable_rows": 4,
              "verified_constituent_num": 28}
        assert svc._sw_amount_rank_invariants(ok) is True

    def test_full_scope_denominator_must_equal_rankable_rows(self) -> None:
        """三轮：全成分正额分母必须**等于**正额行数（不是其子集）。"""
        ok = {"target_amount_rank": 1, "amount_rank_denominator": 4,
              "amount_rank_scope": "full_constituents",
              "amount_rank_evidence": "e", "rankable_rows": 4}
        assert svc._sw_amount_rank_invariants(ok) is True
        bad = {"target_amount_rank": 1, "amount_rank_denominator": 3,
               "amount_rank_scope": "full_constituents",
               "amount_rank_evidence": "e", "rankable_rows": 4}
        assert svc._sw_amount_rank_invariants(bad) is False

    def test_missing_rankable_rows_defaults_to_zero_not_skipped(self) -> None:
        """四轮：缺 ``rankable_rows`` 键按 schema 语义默认 0——全量分母
        一致性与上界检查不再被 get(None) 跳过（反例：缺键 + 全量 denom=3
        曾被误接受）。"""
        bad = {"target_amount_rank": 1, "amount_rank_denominator": 3,
               "amount_rank_scope": "full_constituents",
               "amount_rank_evidence": "e"}
        assert svc._sw_amount_rank_invariants(bad) is False
        ok = {"target_amount_rank": 1, "amount_rank_denominator": 3,
              "amount_rank_scope": "full_constituents",
              "amount_rank_evidence": "e", "rankable_rows": 3}
        assert svc._sw_amount_rank_invariants(ok) is True

    def test_contracts_attached_to_sw_peers_pillar(self) -> None:
        """跨字段不变式以 icontract 契约强制在构建出口（每次调用校验）。"""
        postconditions = getattr(svc._sw_peers_pillar, "__postconditions__", [])
        joined = " ".join(str(getattr(pc, "description", "")) for pc in
                          postconditions)
        assert "amount rank cross-field invariants" in joined
