# -*- coding: utf-8 -*-
"""板块专项报告总预算（issue #81）：

- ``SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS``：独立板块总预算，默认 300s，合法 1—300
  （越界按项目既有 parse_env_float 语义 clamp），不读基本面产品
  ``FUNDAMENTALS_REPORT_TIMEOUT_SECONDS`` 当板块配置。
- :class:`ReportBudget`：同一预算入口（后续趋势/催化票复用），monotonic 截止；
  取数截止（``fetch_deadline``）提前收尾余量，渲染/严格校验/持久化不被取数吃光。
- :func:`bounded_call`：取数/重试/等待钳制剩余取数预算；超时放弃等待并如实标记，
  不伪装硬截止（供应商在途工作可能继续），禁止上下文 join。
- 错误分层（第一轮审查反例）：供应商自身 ``TimeoutError``（Py3.11 与
  ``futures.TimeoutError`` 同类）必须区分于等待超时——前者按局部来源错误
  原样传播走缺口路径，不谎报预算耗尽；每个新出站/重试/退避检查同一取数截止。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pandas as pd
import pytest

import src.services.sector_analysis_service as svc_module
from src.services.sector_analysis_service import (
    BudgetExhausted,
    ReportBudget,
    _ak_with_retry,
    bounded_call,
)


class TestSectorReportTimeoutConfig:
    """SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS 配置字段。"""

    def _config_with_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str | None):
        from src.config import Config

        env_path = tmp_path / ".env"
        lines = ["STOCK_LIST=600519"]
        if value is not None:
            lines.append(f"SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setenv("ENV_FILE", str(env_path))
        # load_dotenv(override=False) 会把首个 env 文件的值固化进 os.environ，
        # 测试间显式 setenv/delenv 隔离（monkeypatch 自动恢复）。
        if value is not None:
            monkeypatch.setenv("SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS", value)
        else:
            monkeypatch.delenv("SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS", raising=False)
        Config.reset_instance()
        try:
            from src.config import get_config

            return get_config()
        finally:
            Config.reset_instance()

    def test_default_300s(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        config = self._config_with_env(monkeypatch, tmp_path, None)
        assert config.sector_analysis_report_timeout_seconds == 300.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        config = self._config_with_env(monkeypatch, tmp_path, "45")
        assert config.sector_analysis_report_timeout_seconds == 45.0

    def test_below_minimum_clamps_to_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        config = self._config_with_env(monkeypatch, tmp_path, "0.2")
        assert config.sector_analysis_report_timeout_seconds == 1.0

    def test_above_maximum_clamps_to_300(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        config = self._config_with_env(monkeypatch, tmp_path, "9999")
        assert config.sector_analysis_report_timeout_seconds == 300.0

    def test_fundamentals_timeout_not_used_as_sector_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """基本面产品的 timeout 不得被读成板块预算（隐式耦合禁止）。"""
        env_path = tmp_path / ".env"
        env_path.write_text(
            "STOCK_LIST=600519\nFUNDAMENTALS_REPORT_TIMEOUT_SECONDS=42\n"
            "SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS=77\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ENV_FILE", str(env_path))
        monkeypatch.setenv("SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS", "77")
        monkeypatch.setenv("FUNDAMENTALS_REPORT_TIMEOUT_SECONDS", "42")
        from src.config import Config, get_config

        Config.reset_instance()
        try:
            config = get_config()
            assert config.sector_analysis_report_timeout_seconds == 77.0
            assert config.fundamentals_report_timeout_seconds == 42.0
        finally:
            Config.reset_instance()


class TestReportBudget:
    """同一预算入口的剩余预算语义（monotonic 截止）。"""

    def test_remaining_and_expired(self):
        budget = ReportBudget(0.2)
        assert budget.remaining() > 0
        assert not budget.expired()
        budget.require()  # 未到期不抛
        time.sleep(0.25)
        assert budget.expired()
        assert budget.remaining() <= 0
        with pytest.raises(BudgetExhausted):
            budget.require()

    def test_deadline_is_absolute_monotonic(self):
        budget = ReportBudget(30.0)
        assert budget.deadline >= time.monotonic()
        assert budget.deadline <= time.monotonic() + 30.0 + 0.5

    def test_total_seconds_recorded(self):
        assert ReportBudget(12.5).total_seconds == 12.5


class TestFetchDeadlineMargin:
    """取数截止 = 主截止 - 收尾余量：渲染/严格校验/持久化不被取数吃光。"""

    def test_fetch_deadline_precedes_deadline_by_margin(self):
        budget = ReportBudget(30.0, finish_margin=5.0)
        assert budget.fetch_deadline < budget.deadline
        assert abs((budget.deadline - budget.fetch_deadline) - 5.0) < 0.1

    def test_zero_margin_keeps_same_deadline(self):
        budget = ReportBudget(30.0)
        assert budget.fetch_deadline == budget.deadline

    def test_require_fetch_raises_when_margin_consumed(self):
        """主预算未耗尽但取数预算（截止-余量）已耗尽：不再发起新出站。"""
        budget = ReportBudget(1.0, finish_margin=5.0)
        assert not budget.expired()  # 主预算还活着
        assert budget.fetch_expired()  # 取数预算已无
        with pytest.raises(BudgetExhausted, match="收尾"):
            budget.require_fetch()

    def test_require_fetch_passes_within_fetch_budget(self):
        budget = ReportBudget(30.0, finish_margin=5.0)
        budget.require_fetch()  # 不抛


class TestBoundedCall:
    """有界调用：等待钳制剩余预算；超时放弃等待不伪装硬截止。"""

    def test_result_within_budget_adopted(self):
        budget = ReportBudget(5.0)
        assert bounded_call(lambda a, b: a + b, 1, 2, budget=budget) == 3

    def test_expired_budget_rejected_before_dispatch(self):
        budget = ReportBudget(0.05)
        time.sleep(0.1)
        calls: list[str] = []

        def _fn() -> str:
            calls.append("called")
            return "late"

        with pytest.raises(BudgetExhausted):
            bounded_call(_fn, budget=budget)
        assert calls == []  # 预算耗尽后不派发新取数

    def test_slow_call_abandoned_when_budget_expires(self):
        budget = ReportBudget(0.2)

        def _slow() -> str:
            time.sleep(2.0)
            return "very late"

        started = time.monotonic()
        with pytest.raises(BudgetExhausted):
            bounded_call(_slow, budget=budget)
        elapsed = time.monotonic() - started
        # 等待被钳制在剩余预算附近，而不是慢调用的完整时长
        assert elapsed < 1.0

    def test_exception_from_call_propagates(self):
        budget = ReportBudget(5.0)

        def _boom() -> None:
            raise RuntimeError("vendor down")

        with pytest.raises(RuntimeError, match="vendor down"):
            bounded_call(_boom, budget=budget)

    def test_vendor_timeout_error_not_misreported_as_budget_exhausted(self):
        """供应商自身 TimeoutError 原样传播（局部来源错误），不谎报预算耗尽。

        Py3.11 futures.TimeoutError 即内建 TimeoutError：初版实现把 fn 内部
        抛出的供应商超时当成等待超时终结为 BudgetExhausted，300 秒预算剩余
        299 秒却报告「预算已耗尽」，丢弃全部有效事实。
        """
        budget = ReportBudget(300.0)

        def _vendor() -> None:
            raise TimeoutError("vendor socket timeout")

        with pytest.raises(TimeoutError, match="vendor socket timeout"):
            bounded_call(_vendor, budget=budget)
        assert budget.remaining() > 299.0  # 预算几乎未消耗，错误非预算耗尽


class TestRetryBudgetClamp:
    """重试器与新出站全部钳制同一取数截止（第一轮审查反例）。"""

    def test_retry_rejects_new_outbound_when_fetch_budget_exhausted(self):
        """取数预算耗尽后，重试器首个出站即拒绝，不派发调用。"""
        budget = ReportBudget(30.0, finish_margin=5.0)
        # 直接吃掉取数预算：主预算仍活着，取数截止（25s 处）已过
        budget.deadline = time.monotonic() + 2.0
        calls: list[str] = []

        def _vendor() -> str:
            calls.append("called")
            return "ok"

        with pytest.raises(BudgetExhausted):
            _ak_with_retry(_vendor, retries=1, delay=0.01, retry_budget=budget)
        assert calls == []  # 晚获得执行权的任务不再新发调用

    def test_no_second_outbound_after_first_call_outlives_fetch_budget(self):
        """首次调用晚于取数截止失败：等待方已放弃，线程内重试不再新发出站。"""
        budget = ReportBudget(6.0, finish_margin=5.0)  # 取数预算 ≈1s
        calls: list[float] = []
        finished = threading.Event()

        def _vendor() -> str:
            calls.append(time.monotonic() - budget.fetch_deadline)
            if len(calls) == 1:
                time.sleep(1.2)  # 晚失败：超过剩余取数预算
                raise RuntimeError("first call failed late")
            finished.set()
            return "ok"

        with pytest.raises(BudgetExhausted):
            bounded_call(_ak_with_retry, _vendor, retries=1, delay=0.01, budget=budget,
                         retry_budget=budget)
        finished.wait(3.0)
        assert len(calls) == 1  # 只有一次真实出站：截止后无第二次
        assert calls[0] < 0  # 首次出站发生在取数截止前

    def test_backoff_sleep_clamped_to_fetch_budget(self):
        """退避时长钳制剩余取数预算，退避后仍无预算则拒绝新出站。"""
        budget = ReportBudget(30.0, finish_margin=5.0)
        budget.deadline = time.monotonic() + 5.2  # 取数预算 ≈0.2s
        attempts: list[str] = []

        def _flaky() -> str:
            attempts.append("try")
            raise RuntimeError("down (fake)")

        started = time.monotonic()
        with pytest.raises(BudgetExhausted):
            _ak_with_retry(_flaky, retries=3, delay=60.0, retry_budget=budget)
        elapsed = time.monotonic() - started
        assert len(attempts) == 1  # 退避后预算已尽：不发起第二次
        assert elapsed < 2.0  # 60s 退避被钳制到剩余预算，不会真睡 60s

    def test_retry_succeeds_within_fetch_budget(self):
        budget = ReportBudget(30.0, finish_margin=5.0)
        attempts: list[str] = []

        def _flaky() -> str:
            attempts.append("try")
            if len(attempts) == 1:
                raise RuntimeError("transient (fake)")
            return "ok"

        assert _ak_with_retry(_flaky, retries=1, delay=0.01, retry_budget=budget) == "ok"
        assert len(attempts) == 2


class TestSwConstituentWorkerStartDeadline:
    """契约 C（第三轮）：候选成分验证的线程内首出站检查同一 fetch 截止。

    真实候选循环 + 真实 Future：worker 已 RUNNING（cancel 不能阻止已运行任务），
    在真正进入供应商调用之前注入调度空档——修复前 vendor 在 fetch 截止后才启动；
    修复后线程内首出站 ``require_fetch`` 拒绝晚执行任务，vendor 不再被启动。
    语义边界如实：不宣称能停止已在途的第三方调用，只保证晚获得执行权的
    出站不再发起；注入点对两种实现形态（直接 vendor / 经 _ak_with_retry）等价。
    """

    def test_preempted_worker_vendor_never_starts_after_fetch_deadline(self, monkeypatch):
        import akshare as ak
        import pandas as pd

        from src.services import sector_analysis_service as svc

        budget = svc.ReportBudget(0.15)
        starts: list[float] = []
        finished = threading.Event()

        def vendor(*args, **kwargs):  # noqa: ANN002,ANN003 - stub 与真实关键字调用形态一致
            starts.append(round(time.monotonic() - budget.fetch_deadline, 4))
            finished.set()
            return pd.DataFrame({"证券代码": ["600519"]})

        original_executor = svc._FETCH_EXECUTOR

        class PreemptedExecutor:
            def submit(self, fn, *args, **kwargs):  # noqa: ANN001
                def run():
                    # RUNNING 后、真正调用供应商之前的调度暂停（0.3s > 0.15s 预算）。
                    if fn is vendor or fn is svc._ak_with_retry:
                        time.sleep(0.3)
                    return fn(*args, **kwargs)

                return original_executor.submit(run)

        tables = {
            "third": pd.DataFrame({"行业名称": ["白酒Ⅲ"], "行业代码": ["851251.SI"],
                                   "上级行业": ["白酒Ⅱ"]}),
            "second": pd.DataFrame({"行业名称": ["白酒Ⅱ"], "上级行业": ["食品饮料"]}),
        }
        monkeypatch.setattr(svc, "_FETCH_EXECUTOR", PreemptedExecutor())
        monkeypatch.setattr(svc, "_SW_TABLES_CACHE", tables)
        monkeypatch.setattr(svc, "_SW_CHAIN_CACHE", {})
        monkeypatch.setattr(ak, "index_component_sw", vendor)
        chain = svc._resolve_sw_chain("600519", "白酒Ⅱ", budget=budget)
        finished.wait(1.0)
        assert starts == []  # vendor 未在 fetch 截止后被线程启动
        assert not chain["l3"]  # 调用方返回空链
        assert chain["gaps"]  # 缺口如实（预算不足中止候选验证）


class TestNonFiniteNumericQualification:
    """契约 D（第三轮）：非有限数不是有效行情——缺失归一，不伪造分位。"""

    def test_num_normalizes_nonfinite_as_missing(self):
        assert svc_module._num(float("nan")) is None
        assert svc_module._num(float("inf")) is None
        assert svc_module._num(float("-inf")) is None
        assert svc_module._num("nan") is None
        assert svc_module._num("Infinity") is None
        assert svc_module._num(None) is None
        assert svc_module._num("") is None
        # 有限数值（含字符串数值）语义不变
        assert svc_module._num(1.65) == 1.65
        assert svc_module._num("-2.1") == -2.1
        assert svc_module._num(3) == 3.0

    def test_momentum_valid_set_excludes_nonfinite_rows(self):
        """同行涨跌幅非有限：动量按有效样本计算，不提供全表名次（记缺口）。

        修正记录（第四轮 spec FAIL1）：原断言把 ``rank_of=4-1=3`` 固化为
        「正确」预期——那是把有效集合统计映射到观测全表名次的伪正确值：
        四板块 [1.65, +inf, -0.5, NaN] 中 1.65 是有效最高值却排 3/4，
        两个缺失板块排在目标之前还是之后不可知。契约收敛后：动量继续按
        有效样本计算，rank_of 只在全表全部有效时输出，同行缺失时为
        None 并在缺口注明有效样本数与原因。
        """
        import pandas as pd

        table = pd.DataFrame(
            {"板块名称": ["白酒Ⅱ", "电池", "银行", "地产"],
             "板块代码": ["BK1277", "BK1033", "BK0475", "BK0476"],
             "涨跌幅": [1.65, float("inf"), -0.5, float("nan")],
             "换手率": [2.3, 2.3, 2.3, 2.3],
             "成交额": [3.1e9, 3.1e9, 3.1e9, 3.1e9],
             "上涨家数": [18, 18, 18, 18], "下跌家数": [2, 2, 2, 2],
             "总市值": [1.9e12] * 4, "领涨股票": ["样本股"] * 4,
             "领涨股票-涨跌幅": [5.5] * 4}
        )
        budget = svc_module.ReportBudget(30.0)
        out = svc_module._prosperity_pillar(
            "白酒Ⅱ", budget=budget, board_code="BK1277", board_table=table)
        # 有效集合 {-0.5, 1.65}：动量按有效样本计算（目标为有效最高 → 100.0）
        assert out["momentum"] == 100.0
        # 有效集合 ≠ 观测全表：不输出无依据的全表名次，缺口注明样本资格
        assert out["rank_of"] is None
        assert out["total_boards"] == 4
        assert any("全表排名" in g for g in out["gaps"])

    def test_target_nonfinite_momentum_and_rank_stay_none(self):
        """目标涨跌幅非有限：不算动量/排名（不伪造 0.0 分位），缺口如实。"""
        import pandas as pd

        table = pd.DataFrame(
            {"板块名称": ["白酒Ⅱ", "电池", "银行"],
             "板块代码": ["BK1277", "BK1033", "BK0475"],
             "涨跌幅": [float("nan"), 1.0, 2.0],
             "换手率": [2.3, 2.3, 2.3],
             "成交额": [3.1e9, 3.1e9, 3.1e9],
             "上涨家数": [18, 18, 18], "下跌家数": [2, 2, 2],
             "总市值": [1.9e12] * 3, "领涨股票": ["样本股"] * 3,
             "领涨股票-涨跌幅": [5.5] * 3}
        )
        budget = svc_module.ReportBudget(30.0)
        out = svc_module._prosperity_pillar(
            "白酒Ⅱ", budget=budget, board_code="BK1277", board_table=table)
        assert out["momentum"] is None
        assert out["rank_of"] is None
        assert out["row"]["change_pct"] is None
        # 其他有效事实保留
        assert out["sector_found"] is True
        assert out["row"]["turnover_rate"] == 2.3
        assert out["breadth"] == 90.0
        assert any("动量" in g for g in out["gaps"])


def _rank_table(changes: list[float], target_index: int = 0) -> pd.DataFrame:
    """n 板块全表，涨跌幅由 ``changes`` 给出，目标默认第一行。"""
    names = [f"板块{i}" for i in range(len(changes))]
    names[target_index] = "白酒Ⅱ"
    codes = [f"BK{i:04d}" for i in range(len(changes))]
    codes[target_index] = "BK1277"
    return pd.DataFrame(
        {"板块名称": names, "板块代码": codes,
         "涨跌幅": changes,
         "换手率": [2.3] * len(changes), "成交额": [3.1e9] * len(changes),
         "上涨家数": [18] * len(changes), "下跌家数": [2] * len(changes),
         "总市值": [1.9e12] * len(changes), "领涨股票": ["样本股"] * len(changes),
         "领涨股票-涨跌幅": [5.5] * len(changes)}
    )


class TestFullTableRankQualification:
    """全表排名资格与公式（issue #81 第四轮 spec FAIL1 收敛契约）。

    - 全表涨跌幅全部有效：名次 = 严格高于目标的个数 + 1（最高=1，并列
      最高同为 1，并列中间按高于个数），不用 total-below 把并列排末。
    - 同行涨跌幅存在缺失/非有限：有效集合 ≠ 观测全表，无法知道缺失板块
      排在目标之前还是之后，不输出无依据的全表名次（rank_of=None + 缺口）。
    - momentum = 严格低于目标的有效样本数 ÷ (有效样本数−1) × 100（并列值
      不计入「低于」数，故并列最高不恒为 100、全相等为 0）；有效样本不足
      2 个时比较样本不足，动量与全表排名保守 N/A 并注明原因。
    """

    def _run(self, changes: list[float]) -> dict:
        budget = svc_module.ReportBudget(30.0)
        return svc_module._prosperity_pillar(
            "白酒Ⅱ", budget=budget, board_code="BK1277",
            board_table=_rank_table(changes))

    def test_all_valid_no_ties_rank_is_one_plus_higher_count(self):
        """全有效无并列：目标 4，全表 [4,5,3] → 1 个更高 → rank 2、momentum 50。"""
        out = self._run([4.0, 5.0, 3.0])
        assert out["rank_of"] == 2
        assert out["total_boards"] == 3
        assert out["momentum"] == 50.0

    def test_all_valid_tie_at_top_share_rank_one(self):
        """全有效并列最高：目标 5，全表 [5,5,3] → 并列同为 rank 1（不排末位）。

        旧 total-below 公式给出 3-1=2，把并列最高错误排到末位。
        """
        out = self._run([5.0, 5.0, 3.0])
        assert out["rank_of"] == 1
        assert out["total_boards"] == 3
        # 有效样本分位：below=1（并列者不计入「低于」数），momentum = 1/2*100
        assert out["momentum"] == 50.0

    def test_all_valid_tie_in_middle_ranks_by_higher_count(self):
        """全有效并列中间：目标 3，全表 [3,5,3,1] → 1 个更高 → rank 2。

        旧 total-below 公式给出 4-1=3，把并列排到唯一更低者之后。
        """
        out = self._run([3.0, 5.0, 3.0, 1.0])
        assert out["rank_of"] == 2
        assert out["total_boards"] == 4
        # 有效样本分位：below=1，momentum = 1/3*100
        assert out["momentum"] == round(1 / 3 * 100, 1)

    def test_peer_missing_suppresses_full_table_rank_with_gap(self):
        """同行缺失（invalid_peers 反例形状）：rank None + 缺口，动量保留。

        [1.65, +inf, -0.5, NaN]：目标 1.65 是有效最高（momentum=100），
        但 2/4 有效样本无法支撑「全表排名」，缺口注明有效样本数与总量。
        """
        out = self._run([1.65, float("inf"), -0.5, float("nan")])
        assert out["rank_of"] is None
        assert out["total_boards"] == 4
        assert out["momentum"] == 100.0
        rank_gaps = [g for g in out["gaps"] if "全表排名" in g]
        assert len(rank_gaps) == 1
        assert "观测 4" in rank_gaps[0]
        assert "有效样本仅 2" in rank_gaps[0]
        # 其他有效事实保留：不因排名资格缺口拖垮整份行情
        assert out["sector_found"] is True
        assert out["row"]["change_pct"] == 1.65
        assert out["breadth"] == 90.0

    def test_single_valid_sample_conservative_na_with_reason(self):
        """边界（第五轮）：观测 4 板块仅目标 1 个有效 → 保守 N/A + 样本不足说明。

        [2.0, +inf, NaN, NaN]：目标 2.0 有效但比较样本不足 2 个，不计算
        动量分、不提供全表排名；缺口注明有效样本数/观测总量与原因，不再
        只留「板块表缺列或数据无效」的误导性通用缺口。
        """
        out = self._run([2.0, float("inf"), float("nan"), float("nan")])
        assert out["rank_of"] is None
        assert out["total_boards"] == 4
        assert out["momentum"] is None
        assert out["row"]["change_pct"] == 2.0
        insuff = [g for g in out["gaps"] if "比较样本不足" in g]
        assert len(insuff) == 1
        assert "观测 4" in insuff[0]
        assert "有效样本仅 1" in insuff[0]
        assert "不计算动量分" in insuff[0] and "不提供全表排名" in insuff[0]
        # 样本不足 ≠ 板块缺列：不得再给误导性通用动量缺口
        assert not any("动量" in g and "缺列或数据无效" in g for g in out["gaps"])
        assert out["sector_found"] is True

    def test_zero_valid_sample_conservative_na_with_reason(self):
        """边界（第五轮）：观测 4 板块涨跌幅全非有限（目标行也无效）→ 同口径说明。

        [NaN, +inf, NaN, +inf]：有效样本 0 个，动量与全表排名保守 N/A，
        缺口注明有效样本数 0/4 与比较样本不足原因——资格说明不依赖目标
        是否有效、不依赖能否计算动量。
        """
        out = self._run([float("nan"), float("inf"), float("nan"), float("inf")])
        assert out["rank_of"] is None
        assert out["total_boards"] == 4
        assert out["momentum"] is None
        assert out["row"]["change_pct"] is None
        insuff = [g for g in out["gaps"] if "比较样本不足" in g]
        assert len(insuff) == 1
        assert "观测 4" in insuff[0]
        assert "有效样本仅 0" in insuff[0]
        assert out["sector_found"] is True
        assert out["breadth"] == 90.0


class TestTrendPeersBudgetPriority:
    """issue #82 预算纪律：必需新块先于可选长链；趋势降块不拖垮收尾。

    - 编排顺序：趋势/同业出站先于申万参考链/概念（可选阶段不得吃完
      取数预算让必需块根本不尝试）
    - 趋势块取数预算耗尽：只降趋势块（块内缺口），compose 不上抛，
      已取得内容保留走总截止收尾
    """

    def _compose_with_order_capture(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        order: list[str] = []

        def _ident(code, *, budget):
            return {
                "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
                "board_name": "白酒Ⅱ", "board_code": "BK1277",
                "subject_status": "confirmed",
                "membership": {"verified": True, "member_count": 20},
                "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
                "classification_system": "东财行业板块", "classification_level": None,
                "classification_version": None,
                "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
                "sector_name": "白酒Ⅱ", "gaps": [], "_board_table": None,
                "_cons_snapshot": pd.DataFrame(
                    {"代码": ["600519"], "名称": ["贵州茅台"],
                     "涨跌幅": [1.65], "成交额": [3.1e9]}),
                "_cons_observed_at": "2026-11-05T14:00:01",
            }

        def _trend(code, name, *, budget, subject_confirmed=False, current_time=None):
            order.append("trend")
            return {"eligible": subject_confirmed, "window_completed_sessions": 20,
                    "interval_count": 19, "start_date": None, "end_date": None,
                    "expected_dates": [], "period_return_pct": None, "benchmark": None,
                    "relative_return_pp": None, "source": None,
                    "observed_at": "2026-11-05T14:00:02", "gaps": []}

        def _peers(code, cons, *, budget, snapshot_observed_at=None,
                   subject_confirmed=False):
            order.append("peers")
            return {"eligible": subject_confirmed,
                    "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
                    "items": [], "target_in_amount_top5": None,
                    "target_change_pct_rank": None, "sample_size": 0,
                    "snapshot_observed_at": snapshot_observed_at, "source": None,
                    "gaps": []}

        def _sw(code, text, *, budget):
            order.append("sw")
            return {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []}

        def _concepts(code, *, budget):
            order.append("concepts")
            return {"items": [], "source": "ifind", "total_returned": 0,
                    "truncated": False, "gaps": []}

        monkeypatch.setattr(svc_module, "_identify_sector", _ident)
        monkeypatch.setattr(svc_module, "_sector_trend_pillar", _trend)
        monkeypatch.setattr(svc_module, "_peers_pillar", _peers)
        monkeypatch.setattr(svc_module, "_resolve_sw_chain", _sw)
        monkeypatch.setattr(svc_module, "_concepts_pillar", _concepts)
        monkeypatch.setattr(
            svc_module, "_policy_pillar",
            lambda text: {"lean": None, "label": "未知", "gaps": []})
        monkeypatch.setattr(
            svc_module, "_base_rate_pillar",
            lambda text: {"base_rate": 0.5, "basis": "neutral_default",
                          "hit_keyword": "", "is_default": True,
                          "table_version": "v1.0", "table_updated": "2026-09-29",
                          "table_industry_count": 21, "percentile": None,
                          "all_rates": []})
        monkeypatch.setattr(
            svc_module, "_prosperity_pillar",
            lambda name, *, budget, board_code=None, board_table=None: {
                "sector_found": True, "momentum": 50.0, "activity": 50.0,
                "amount_activity": 50.0, "breadth": 50.0, "rank_of": 1,
                "total_boards": 2, "row": {}, "gaps": []})
        return order

    def test_required_blocks_precede_optional_long_chains(self, monkeypatch: pytest.MonkeyPatch):
        """趋势/同业（必需）先于申万参考链与概念（可选长链）执行。"""
        order = self._compose_with_order_capture(monkeypatch)
        payload = svc_module.compose_report("600519", "贵州茅台", budget=ReportBudget(30.0))
        self_idx_trend = order.index("trend")
        self_idx_peers = order.index("peers")
        assert self_idx_trend < order.index("sw")
        assert self_idx_trend < order.index("concepts")
        assert self_idx_peers < order.index("sw")
        assert self_idx_peers < order.index("concepts")
        # 载荷仍完整（收尾未受可选链影响）
        assert payload["sector_trend"]["eligible"] is True

    def test_trend_fetch_budget_exhausted_degrades_block_not_report(self, monkeypatch: pytest.MonkeyPatch):
        """趋势出站预算耗尽：块内缺口，compose 返回（不上抛 BudgetExhausted）。"""
        import akshare as ak

        def _ident(code, *, budget):
            return {
                "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
                "board_name": "白酒Ⅱ", "board_code": "BK1277",
                "subject_status": "confirmed",
                "membership": {"verified": True, "member_count": 20},
                "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
                "classification_system": "东财行业板块", "classification_level": None,
                "classification_version": None,
                "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
                "sector_name": "白酒Ⅱ", "gaps": [], "_board_table": None,
                "_cons_snapshot": None, "_cons_observed_at": None,
            }

        monkeypatch.setattr(svc_module, "_identify_sector", _ident)
        monkeypatch.setattr(svc_module, "_resolve_sw_chain",
                            lambda code, text, *, budget: {
                                "l1": "", "l2": "", "l3": "", "l3_code": "",
                                "gaps": ["预算不足，参考链未取得"]})
        monkeypatch.setattr(
            svc_module, "_policy_pillar",
            lambda text: {"lean": None, "label": "未知", "gaps": []})
        monkeypatch.setattr(
            svc_module, "_base_rate_pillar",
            lambda text: {"base_rate": 0.5, "basis": "neutral_default",
                          "hit_keyword": "", "is_default": True,
                          "table_version": "v1.0", "table_updated": "2026-09-29",
                          "table_industry_count": 21, "percentile": None,
                          "all_rates": []})
        monkeypatch.setattr(
            svc_module, "_prosperity_pillar",
            lambda name, *, budget, board_code=None, board_table=None: {
                "sector_found": True, "momentum": 50.0, "activity": 50.0,
                "amount_activity": 50.0, "breadth": 50.0, "rank_of": 1,
                "total_boards": 2, "row": {}, "gaps": []})
        monkeypatch.setattr(
            svc_module, "_concepts_pillar",
            lambda code, *, budget: {"items": [], "source": "ifind",
                                     "total_returned": 0, "truncated": False,
                                     "gaps": []})

        def _exhausted(**kwargs):
            raise BudgetExhausted("取数预算已耗尽")

        monkeypatch.setattr(ak, "stock_board_industry_hist_em", _exhausted)
        monkeypatch.setattr(ak, "stock_zh_index_daily_em", _exhausted)
        budget = ReportBudget(30.0, finish_margin=5.0)
        payload = svc_module.compose_report("600519", "贵州茅台", budget=budget)
        trend = payload["sector_trend"]
        assert trend["eligible"] is True
        assert trend["period_return_pct"] is None
        assert any("预算" in g for g in trend["gaps"])
        # 已取得内容保留：身份/行情完整，completeness partial 但报告未上抛
        assert payload["identity"]["subject_status"] == "confirmed"
        assert payload["assessment"]["completeness"] == "partial"
        assert any("板块近期趋势未取得" in r for r in payload["assessment"]["reasons"])
        # 可选长链（申万/概念）照常完成自己的降级，不影响主体；
        # 同业：资格满足（confirmed）但快照未取得 → eligible=True + 样本空 + 数据缺口
        peers = payload["sector_peers"]
        assert peers["eligible"] is True
        assert peers["items"] == []
        assert any("成分快照未取得" in g for g in peers["gaps"])
