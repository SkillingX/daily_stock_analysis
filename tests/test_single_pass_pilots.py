# -*- coding: utf-8 -*-
"""issue #56 试点契约：技术与资金研究员代码备料、单轮无工具分析、失败隔离。

覆盖验收反例：局部技术缺项仍解释、仅资金流合格即完成、资金不合格不启动模型、
金额单位不明不推断、同报告取数去重（issue #64：机构/股东侧退役，机构不再是
资金研究员的启动条件、解释来源或评分依据）；
共享事实身份与既有 #42/#51 契约由各自测试文件继续保护。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.agent.deep_research import explore_agents
from src.config import Config
from src.core.research_run import ResearchRun
from src.deep_research_dims.context import SharedContext

# 复用 Choice 原生资金流冻结夹具（真实交易日历与 NETINFLOW 传输）
from tests.test_choice_capital_flow import native_flow  # noqa: E402
# 复用 loopback provider 与 ledger 环境（真实准入/传输/解析/保存，issue #55 夹具；
# usage_provider 作为 ledger_env 的依赖须在本模块命名空间可见）
from tests.test_model_attempt_ledger import ledger_env, usage_provider  # noqa: E402,F401


def _history(days: int, price: float = 10.0) -> list[dict[str, Any]]:
    return [
        {"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": price, "high": price + 1,
         "low": price - 1, "close": price, "volume": 1000}
        for i in range(days)
    ]


def _success_model(content: str, calls: list[dict[str, Any]] | None = None):
    def generate(**kwargs: Any):
        if calls is not None:
            calls.append(kwargs)
        return SimpleNamespace(success=True, content=content, total_steps=1, error=None)

    return generate


class TestTechnicalSinglePass:
    def test_no_valid_material_skips_model(self, monkeypatch):
        """行情、日线、指标、结构全缺失时不发模型请求。"""
        import pandas as pd

        monkeypatch.setattr(
            "src.services.history_loader.load_history_df",
            lambda *a, **k: (None, "none"),
        )
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model("{}", calls))
        result = explore_agents.run_technical_agent("600519", "贵州茅台", object())
        assert calls == []
        assert result["ok"] is False
        assert "未启动模型" in result["error"]
        assert pd is not None  # pandas 仅在指标路径导入，此处只保证环境可用

    def test_partial_history_still_explains_once_without_tools(self, monkeypatch):
        """仅有短日线（指标/结构缺项）时保留解释机会，且只发一次无工具请求。"""
        monkeypatch.setattr(
            "src.services.history_loader.load_history_df",
            lambda *a, **k: (None, "none"),
        )
        calls: list[dict[str, Any]] = []
        content = (
            '{"chanlun_summary": "结构资料缺失，无法判定缠论结构", "support": 9.0, "resistance": 11.0,'
            ' "indicator_summary": "样本不足，MACD/RSI 未取得", "wave_note": "不适用",'
            ' "score": 50, "basis": "仅 10 根日线收盘区间", "narrative": "日线 10 根，样本不足。"}'
        )

        def short_history(stock_code: str, days: int = 250):
            import pandas as pd

            return pd.DataFrame(_history(10)), "fixture"

        monkeypatch.setattr(
            "src.services.history_loader.load_history_df", short_history
        )
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model(content, calls))
        result = explore_agents.run_technical_agent("600519", "贵州茅台", object())
        assert len(calls) == 1
        loop_kwargs = calls[0]
        assert loop_kwargs["max_steps"] == 1
        assert loop_kwargs["tool_registry"].to_openai_tools() == []
        task_text = loop_kwargs["messages"][-1]["content"]
        assert "技术面深度分析" in task_text
        assert "不要请求任何工具" in task_text
        assert "MACD/RSI/均线指标" in task_text  # 缺口按名披露
        assert result["ok"] is True
        assert result["data"]["score"] == 50

    def test_source_only_text_is_not_valid_material(self):
        """只有来源文字（无任何结构化事实）不算可解释资料。"""
        empty_ctx = SharedContext("600519", "贵州茅台", "2026-10-23T15:00:00")
        facts = explore_agents._shared_market_facts(empty_ctx)
        assert facts  # 来源文字存在……
        valid = explore_agents._technical_valid_parts(empty_ctx, [], "", "")
        assert not any(valid.values())  # ……但不构成有效资料

    def test_failed_supplement_keeps_stage_zero_partial_history(self, monkeypatch):
        """补取失败不得丢弃阶段 0 已取得的部分日线（issue #56 反例契约）。"""
        run = ResearchRun(timeout_seconds=60.0)
        run.context = SharedContext("600519", "贵州茅台", "2026-10-15T16:00:00")
        run.context.history = [
            {"date": f"2026-10-{1 + i:02d}", "open": 96 + i, "high": 99 + i,
             "low": 95 + i, "close": 98 + i, "volume": 1000}
            for i in range(10)
        ]
        run.context.history_source = "stage-zero-valid-partial"
        load_calls: list[str] = []

        def failing_loader(*a: Any, **k: Any):
            load_calls.append("600519")
            return None, "none"

        monkeypatch.setattr("src.services.history_loader.load_history_df", failing_loader)
        calls_model: list[dict[str, Any]] = []
        content = (
            '{"chanlun_summary": "样本不足，结构未取得", "support": 90, "resistance": 110,'
            ' "indicator_summary": "样本不足，MACD/RSI 未取得", "wave_note": "不适用",'
            ' "score": 50, "basis": "仅 10 根日线", "narrative": "日线 10 根，样本不足。"}'
        )
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model(content, calls_model))
        with run.bind():
            result = explore_agents.run_technical_agent("600519", "贵州茅台", object())
        assert len(load_calls) == 1  # 一次有界补取，失败不重复出站
        assert result["ok"] is True  # 部分日线保留解释机会
        task_text = calls_model[0]["messages"][-1]["content"]
        assert "107" in task_text and "2026-10-10" in task_text
        assert "stage-zero-valid-partial" in task_text

    def test_shared_history_reused_without_refetch(self, monkeypatch):
        """共享快照 ≥60 根时单飞日线不再调用加载入口。"""
        run = ResearchRun(timeout_seconds=60.0)
        run.context = SharedContext("600519", "贵州茅台", "2026-10-23T15:00:00")
        run.context.history = _history(260)
        with run.bind():
            with monkeypatch.context() as m:
                m.setattr(
                    "src.services.history_loader.load_history_df",
                    lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应重复取数")),
                )
                history, _source = explore_agents._prepared_history("600519")
        assert len(history) == 260

    def test_prepared_history_single_flight(self, monkeypatch):
        """同 run 缺日线时并发补取只执行一次加载。"""
        config = Config(data_source_profile="choice", deep_research_cross_validate=False)
        monkeypatch.setattr(Config, "_instance", config)
        run = ResearchRun(timeout_seconds=60.0)
        load_calls: list[int] = []

        def counting_loader(*a: Any, **k: Any):
            load_calls.append(1)
            import pandas as pd

            return pd.DataFrame(_history(100)), "fixture"

        monkeypatch.setattr(
            "src.services.history_loader.load_history_df", counting_loader
        )
        with run.bind():
            first, _s1 = explore_agents._prepared_history("600519")
            second, _s2 = explore_agents._prepared_history("600519")
        assert len(load_calls) == 1
        assert len(first) == len(second) == 100


class TestCapitalSinglePass:
    def _bind_run(self, monkeypatch):
        config = Config(data_source_profile="choice", deep_research_cross_validate=False)
        monkeypatch.setattr(Config, "_instance", config)
        return ResearchRun(timeout_seconds=60.0)

    def test_only_flow_valid_starts_model_and_scores_from_flow(self, native_flow, monkeypatch):
        """issue #64：机构侧退役——资金流三窗口合格即完成，资金总分 = 合格 flow_score。"""
        from src.agent.tools import data_tools

        def no_institution(code, **kwargs):
            raise AssertionError("机构块取数已退役，不得调用")

        monkeypatch.setattr(data_tools._get_fetcher_manager(), "get_fundamental_context", no_institution)
        content = (
            '{"flow_summary": "主力净流入为正", "flow_score": 72,'
            ' "institution_summary": "暂无法判定", "institution_score": null,'
            ' "score": null, "narrative": "主力净流入为正，机构资料未核实。"}'
        )
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model(content, calls_model))
        result = explore_agents.run_capital_agent("300260", "新莱应材", object())
        assert len(calls_model) == 1
        assert result["ok"] is True
        data = result["data"]
        assert data.get("status") != "degraded"
        # 模型输出的机构/合成字段一律丢弃，不再构成降级原因或评分
        assert data["flow_score"] == 72 and data["score"] == 72
        assert data.get("institution_score") is None and data.get("institution_summary") is None
        assert "Choice 主力净流入" in data["flow_summary"]

    @staticmethod
    def _flow_stub(payload: dict[str, Any]):
        """既有资金流入口替身：签名对齐当前接缝并锁零行业榜出站（issue #64）。"""
        def flow(code: str, *, include_sector_flows: bool = True) -> dict[str, Any]:
            assert include_sector_flows is False  # 深研资金路径零行业榜出站
            return {"stock_code": code, **payload}
        return flow

    # 否则有效的完整三窗口基线：单位反例保持唯一缺口，门控不会因窗口/日期缺失
    # 顺带关闭（根审查修正：单位反例必须被隔离）。
    _COMPLETE_WINDOWS = {
        "status": "ok",
        "main_net_inflow": 100.0, "inflow_5d": 250.0, "inflow_10d": 250.0,
        "expected_date": "2026-10-13", "as_of_date": "2026-10-13",
        "coverage": {str(n): {"complete": True} for n in (1, 5, 10)},
        "errors": [],
    }

    def test_unknown_unit_never_infers_amounts(self, monkeypatch):
        """三窗口/日期完整但单位不明：不算有效资金资料，不推断金额与方向。"""
        from src.agent.tools import data_tools

        config = Config(data_source_profile="choice", deep_research_cross_validate=False)
        monkeypatch.setattr(Config, "_instance", config)
        monkeypatch.setattr(
            data_tools,
            "_handle_get_capital_flow",
            self._flow_stub(dict(self._COMPLETE_WINDOWS, field_meta={})),
        )
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model("{}", calls_model))
        result = explore_agents.run_capital_agent("300260", "新莱应材", object())
        assert calls_model == []
        assert result["ok"] is False
        assert "单位未确认" in result["error"]

    def test_unit_conflict_veto_beats_source_unit_declaration(self, monkeypatch):
        """资格层明确拒绝（单位冲突）一票否决，source_unit 申报不推翻。"""
        from src.agent.tools import data_tools

        config = Config(data_source_profile="choice", deep_research_cross_validate=False)
        monkeypatch.setattr(Config, "_instance", config)
        monkeypatch.setattr(
            data_tools,
            "_handle_get_capital_flow",
            self._flow_stub(dict(self._COMPLETE_WINDOWS, status="partial", source_unit="CNY",
                                 errors=["unit_conflict"],
                                 field_meta={"main_inflow": {"rule_eligible": False,
                                                             "normalization_error": "unit_conflict"}})),
        )
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model("{}", calls_model))
        result = explore_agents.run_capital_agent("300260", "新莱应材", object())
        assert calls_model == []
        assert result["ok"] is False
        assert "unit_conflict" in result["error"]

    def test_capital_flow_single_flight_within_report(self, native_flow, monkeypatch):
        """同报告资金流需求并发/重复只出站一次（#51 single_fetch 复用）。"""
        from src.agent.tools import data_tools

        run = self._bind_run(monkeypatch)
        handler_calls: list[str] = []
        real_handler = data_tools._handle_get_capital_flow

        def counting(code: str, **kwargs: Any):
            handler_calls.append(code)
            assert kwargs == {"include_sector_flows": False}  # 深研路径零行业榜出站
            return real_handler(code, **kwargs)

        monkeypatch.setattr(data_tools, "_handle_get_capital_flow", counting)
        with run.bind():
            first = explore_agents._capital_flow_material("300260")
            second = explore_agents._capital_flow_material("300260")
        assert handler_calls == ["300260"]
        assert first is not None and second is not None
        assert first["main_net_inflow"] == second["main_net_inflow"]

    # 夹具对齐 tests/fixtures/model_workflow_baseline.json sufficient 样本的
    # total_mv/circ_mv 资格记录（frozen-choice 单源、currency_base、CNY）。
    QUALIFIED_CAP_META = {
        "source": "frozen-choice", "value": 2050000000000.0,
        "unit": "currency_base", "currency": "CNY",
        "quality": {"status": "single_source"}, "rule_eligible": True,
    }

    def _cap_run(self, monkeypatch, field_meta: dict[str, Any]) -> Any:
        """绑定带机构块与指定市值 field_meta 的共享上下文（零新增出站路径）。"""
        run = self._bind_run(monkeypatch)
        run.context = SharedContext("600519", "贵州茅台", "2026-10-15T16:00:00")
        run.context.fundamental = {
            "market_cap": 2050000000000.0,
            "field_meta": field_meta,
            "institution": {"status": "ok", "data": {"institution_holding_change": -1.25},
                            "field_meta": {"institution_holding_change": {"rule_eligible": True, "period": "2026中报"}}},
        }
        return run

    def _guard_fetch(self, monkeypatch, fetch_calls: list[str]) -> None:
        from src.agent.tools import data_tools

        def no_fetch(code, **k):
            fetch_calls.append(code)
            return {}

        monkeypatch.setattr(data_tools._get_fetcher_manager(), "get_fundamental_context", no_fetch)

    def test_qualified_market_cap_reaches_capital_task(self, native_flow, ledger_env, monkeypatch):
        """同报告合格市值锚点（整条资格记录）经真实执行链到达实际 HTTP 请求。

        不 mock 模型执行/传输/usage/解析/保存：run_capital_agent → 原
        run_agent_loop → LLMToolAdapter → litellm 准入 → loopback provider
        （issue #55 usage_provider 夹具）。观察只走产品 _attach_observer 的
        pre_api_call 被动旁路，产品 observer 保留为原 logger；旧版 monkeypatch
        run_agent_loop 只证明任务文本拼接，本测试证明整条资格记录真正出站。
        """
        import json

        from src.llm import concurrency
        from src.llm.attempt import STATUS_OK

        # 记录字段对齐装配层资格包（时点/财期/口径/来源族/质量拒绝码齐备），
        # 下面按确切 JSON record 等值断言（非字段名字符串）验证不丢证据。
        qualified = dict(
            self.QUALIFIED_CAP_META,
            field="total_mv", caliber="snapshot", period=None, period_basis="unknown",
            source_family="frozen-local-fixture", observed_at="2026-10-15T15:00:00+08:00",
            quality={"status": "single_source", "reason_codes": ["single_source_unverified"]},
        )
        total_record = dict(qualified)
        circ_record = dict(qualified, field="circ_mv", value=2000000000000.0)
        adapter, state = ledger_env
        # ledger_env 配置保住真实 loopback model 路由；资金流输入是 choice
        # 原生映射（native_flow 设计环境），只在实例上补 profile，不重建
        # Config，避免 _cap_run 式重设导致真实 model 路由漂移。
        monkeypatch.setattr(Config._instance, "data_source_profile", "choice")
        run = ResearchRun(timeout_seconds=60.0)
        run.context = SharedContext("600519", "贵州茅台", "2026-10-15T16:00:00")
        run.context.fundamental = {
            "market_cap": 2050000000000.0,
            "field_meta": {"total_mv": total_record, "circ_mv": circ_record},
            "institution": {"status": "ok", "data": {"institution_holding_change": -1.25},
                            "field_meta": {"institution_holding_change": {"rule_eligible": True, "period": "2026中报"}}},
        }
        fetch_calls: list[str] = []
        self._guard_fetch(monkeypatch, fetch_calls)
        outbound: list[dict[str, Any]] = []
        attach = concurrency._attach_observer

        def observe_attach(kwargs: dict[str, Any], attempt: Any) -> None:
            attach(kwargs, attempt)  # 产品 observer 原样安装
            original = kwargs["logger_fn"]

            def observer(details: dict[str, Any]) -> None:
                original(details)  # 保留原 logger，只追加被动观察
                if details.get("log_event_type") != "pre_api_call":
                    return
                body = (details.get("additional_args") or {}).get("complete_input_dict")
                if isinstance(body, dict):
                    outbound.append(body)

            kwargs["logger_fn"] = observer

        monkeypatch.setattr(concurrency, "_attach_observer", observe_attach)
        with run.bind():
            result = explore_agents.run_capital_agent("600519", "贵州茅台", adapter)
        assert state["count"] == 1  # exactly 1 实际 HTTP 出站
        assert len(run.attempts) == 1 and run.attempts[0].outbound is True
        assert run.attempts[0].status == STATUS_OK
        assert len(outbound) == 1
        system_text = str(outbound[0]["messages"][0].get("content") or "")
        assert "共享宪法" in system_text  # 既有宪法注入不回退（来源/缺口纪律）
        assert "本轮职责边界" in system_text  # 试点分区边界真实组合出站
        assert "时点纪律" in system_text  # 时点职责真实出站：以给定时点/财期解读，不声称全局最新
        # 打包 capital SKILL 现行规则真实到达实际 HTTP 出站（隔离源副本无本地
        # capital 覆盖；真实报告曾因 prompt 缺失叙述「行业资金排名缺失」）。
        assert "资金总分" in system_text
        assert "机构/股东侧" in system_text and "退役" in system_text
        task_text = "\n".join(str(m.get("content") or "") for m in outbound[0].get("messages", []))
        # 退役行业榜整键不在实际出站任务（真实报告曾注入空占位被模型叙述为缺失）
        assert '"sector_rankings"' not in task_text
        flow_section = task_text.split("1) 资金流资料", 1)[1].split("\n3) 市值锚点", 1)[0]
        flow_payload = json.loads(flow_section[flow_section.index("{"):flow_section.rindex("}") + 1])
        assert "sector_rankings" not in flow_payload
        # 字段资格记录质量口径经投影后确切等值（single_source/single_family 不回退 unverified）
        assert flow_payload["field_meta"]["main_inflow"]["quality"] == {
            "status": "single_source", "reason_codes": ["single_family"],
        }
        section = task_text.split("3) 市值锚点", 1)[1].split("\n4)", 1)[0]
        payload = json.loads(section[section.index("{"):])
        # 整条资格记录确切等值：总/流通市值各自到达实际请求，时点/口径/来源族/质量不丢
        assert payload["anchors"]["total"] == {"label": "总市值", "field": "total_mv", "record": total_record}
        assert payload["anchors"]["circ"] == {"label": "流通市值", "field": "circ_mv", "record": circ_record}
        assert "gaps" not in payload  # 两项均合格，无缺口包
        assert payload["anchors"]["total"]["record"]["quality"]["status"] == "single_source"  # single_source 不变
        assert fetch_calls == []  # 锚点全部来自共享上下文，同报告零新增取数
        assert result["ok"] is True  # provider status=ok 经现有 parse 成功
        # issue #64：固定 provider 响应无 flow_score/分析正文——如实降级，不冒充模型质量分
        assert result["data"]["status"] == "degraded"

    def test_market_cap_alias_entry_not_dropped(self, native_flow, monkeypatch):
        """合格记录用现有支持的别名键名（market_cap）时同样不得漏传。"""
        run = self._cap_run(monkeypatch, {
            "market_cap": dict(self.QUALIFIED_CAP_META, field="market_cap"),
        })
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model(
            '{"flow_summary": "s", "flow_score": 72, "institution_summary": "s", "institution_score": 55,'
            ' "score": 60, "narrative": "n"}', calls_model))
        with run.bind():
            explore_agents.run_capital_agent("600519", "贵州茅台", object())
        task_text = calls_model[0]["messages"][-1]["content"]
        assert "2050000000000.0" in task_text  # 已有合格记录不因键名漏传
        assert '"total"' in task_text and "总市值" in task_text
        assert "流通市值" in task_text and "null" in task_text  # 流通市值无记录如实标缺

    def test_unqualified_market_cap_keeps_real_gap(self, native_flow, monkeypatch):
        """拒绝记录/裸标量不补造资格：不合格记录整条按拒绝原因如实保留。"""
        run = self._cap_run(monkeypatch, {
            "total_mv": {"field": "total_mv", "value": 2050000000000.0, "unit": "currency_base",
                          "currency": "CNY", "rule_eligible": False, "normalization_error": "unit_conflict"},
        })
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model(
            '{"flow_summary": "s", "flow_score": 72, "institution_summary": "s", "institution_score": 55,'
            ' "score": 60, "narrative": "n"}', calls_model))
        with run.bind():
            result = explore_agents.run_capital_agent("600519", "贵州茅台", object())
        assert result["ok"] is True
        task_text = calls_model[0]["messages"][-1]["content"]
        assert '"anchors": {}' in task_text  # 不合格记录不作为量级锚点
        assert "unit_conflict" in task_text and "rule_eligible" in task_text  # 拒绝记录整条保留（含原值）
        assert "流通市值" in task_text and "null" in task_text  # 无记录的流通市值如实标缺

    def test_missing_cap_records_placeholder_not_fabricated(self, native_flow, monkeypatch):
        """共享上下文无市值资格记录时不注入锚点数值，也不虚构数字。"""
        run = self._cap_run(monkeypatch, {})
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model(
            '{"flow_summary": "s", "flow_score": 72, "institution_summary": "s", "institution_score": 55,'
            ' "score": 60, "narrative": "n"}', calls_model))
        with run.bind():
            result = explore_agents.run_capital_agent("600519", "贵州茅台", object())
        assert result["ok"] is True
        task_text = calls_model[0]["messages"][-1]["content"]
        assert "2050000000000.0" not in task_text  # 裸标量不作为锚点传递
        assert "总市值" in task_text and "流通市值" in task_text  # 两项均无记录按真实缺口说明

    def test_market_cap_alone_never_starts_capital_model(self, monkeypatch):
        """资金流无效时即使市值锚点合格也不启动资金模型（门控不变）。"""
        from src.agent.tools import data_tools

        config = Config(data_source_profile="choice", deep_research_cross_validate=False)
        monkeypatch.setattr(Config, "_instance", config)
        monkeypatch.setattr(
            data_tools,
            "_handle_get_capital_flow",
            self._flow_stub({"status": "ok", "main_net_inflow": None,
                             "inflow_5d": None, "inflow_10d": None, "errors": []}),
        )
        run = ResearchRun(timeout_seconds=60.0)
        run.context = SharedContext("600519", "贵州茅台", "2026-10-15T16:00:00")
        run.context.fundamental = {
            "market_cap": 2050000000000.0,
            "field_meta": {"total_mv": dict(self.QUALIFIED_CAP_META, field="total_mv")},
        }
        calls_model: list[dict[str, Any]] = []
        monkeypatch.setattr(explore_agents, "run_agent_loop", _success_model("{}", calls_model))
        from src.core.research_run import research_dimension

        token = research_dimension.set("capital")
        try:
            with run.bind():
                result = explore_agents.run_capital_agent("600519", "贵州茅台", object())
        finally:
            research_dimension.reset(token)
        assert calls_model == []
        assert result["ok"] is False
        assert "未启动模型" in result["error"]
        # 门控早退不丢已知市值：备料留存先于早退执行（issue #56 A2 全入口）
        assert "总市值" in {fact["label"] for fact in run.facts.get("capital", [])}


class TestPreparedFactRetention:
    """issue #56 A2：调用模型前已取得/已计算的合格标量必须进 ResearchRun 留存。

    模型失败/格式修复失败/取消后，退化报告沿既有 retained_facts 渲染机制
    保留真实已知资料（含来源/单位/资格与拒绝原因）；分数保持 null，不假造
    模型分析。真实 HTTP 拒绝 + 保存/历史读取链路由
    tests/test_report_usage_attribution.py 的 reject 用例覆盖。
    """

    def _bound_run(self, monkeypatch) -> ResearchRun:
        config = Config(data_source_profile="choice", deep_research_cross_validate=False)
        monkeypatch.setattr(Config, "_instance", config)
        run = ResearchRun(timeout_seconds=60.0)
        run.context = SharedContext("600519", "贵州茅台", "2026-10-15T16:00:00")
        return run

    @staticmethod
    def _failing_model(error: str):
        return lambda **kwargs: SimpleNamespace(success=False, content="", total_steps=0, error=error)

    def test_technical_failure_retains_computed_facts(self, monkeypatch):
        """模型失败后已算指标/支撑阻力/缠论结构与日线/行情标量都在留存中。"""
        from src.core.research_run import research_dimension

        run = self._bound_run(monkeypatch)
        run.context.history = _history(260, price=10.0)
        run.context.history_source = "fixture-source"
        run.context.quote = {"price": 10.5}
        monkeypatch.setattr(explore_agents, "run_agent_loop", self._failing_model("Agent timed out"))
        token = research_dimension.set("technical")
        try:
            with run.bind():
                result = explore_agents.run_technical_agent("600519", "贵州茅台", object())
        finally:
            research_dimension.reset(token)
        assert result["ok"] is False
        by_label = {fact["label"]: fact for fact in run.facts["technical"]}
        assert {"20日均线", "6日RSI", "最近交易日收盘", "价格", "支撑", "阻力", "缠论趋势", "缠论位置"} <= set(by_label)
        assert by_label["20日均线"]["tool"] == "analyze_trend"
        # 区分计算者与上游日线来源，保留补取结果的已知来源与真实末日
        assert by_label["20日均线"]["source"] == "代码指标计算，日线来源 fixture-source"
        assert by_label["20日均线"]["period"] == "2026-10-08"  # _history(260) 真实末日
        assert by_label["最近交易日收盘"]["source"] == "fixture-source"
        assert by_label["支撑"]["source"] == "代码计算，日线来源 fixture-source"
        assert by_label["支撑"]["value"] == "9.0" and by_label["阻力"]["value"] == "11.0"
        assert by_label["缠论趋势"]["source"] == "缠论引擎计算"
        assert by_label["缠论趋势"]["value"] == "盘整"
        assert "缠论背驰" not in by_label  # 引擎未给出的结论不补造
        prices = {fact["value"] for fact in run.facts["technical"] if fact["label"] == "价格"}
        assert prices == {"10.0", "10.5"}  # 分析器现价（最新收盘）与共享行情现价都保留
        assert by_label["20日均线"]["value"] == by_label["5日均线"]["value"] == "10.0"

    def test_technical_short_history_never_retains_default_indicators(self, monkeypatch):
        """局部样本不足：分析器 0 默认值不当实际指标留存，日线标量照常保留。"""
        from src.core.research_run import research_dimension

        run = self._bound_run(monkeypatch)
        run.context.history = _history(10)
        run.context.history_source = "fixture-source"
        # 冻结日线补取边界（阶段 0 部分日线 + 补取失败保留既有资料的契约路径）。
        monkeypatch.setattr(
            "src.services.history_loader.load_history_df", lambda *a, **k: (None, "none")
        )
        monkeypatch.setattr(explore_agents, "run_agent_loop", self._failing_model("timeout"))
        token = research_dimension.set("technical")
        try:
            with run.bind():
                result = explore_agents.run_technical_agent("600519", "贵州茅台", object())
        finally:
            research_dimension.reset(token)
        assert result["ok"] is False
        labels = {fact["label"] for fact in run.facts["technical"]}
        assert labels <= {"最近交易日开盘", "最近交易日最高", "最近交易日最低", "最近交易日收盘",
                          "最近交易日成交量", "支撑", "阻力"}
        assert not labels & {"5日均线", "20日均线", "6日RSI", "MACD DIF", "缠论趋势", "缠论位置"}
        assert "最近交易日收盘" in labels  # 已取得的日线标量不因样本不足丢失

    def test_capital_failure_retains_market_caps_and_flow(self, native_flow, monkeypatch):
        """模型失败后合格市值与资金流标量全部留在留存中，资格标注完整。

        issue #64：机构侧退役——共享上下文中的合格机构记录是既有公司资料，
        资金研究员不再读取，其字段不得进入资金留存事实。
        """
        from src.core.research_run import research_dimension

        run = self._bound_run(monkeypatch)
        qualified = dict(self._qualified_cap_meta(), field="total_mv")
        run.context.fundamental = {
            "market_cap": 2050000000000.0,
            "field_meta": {"total_mv": qualified, "circ_mv": dict(qualified, field="circ_mv", value=2000000000000.0)},
            "institution": {"status": "ok", "data": {"institution_holding_change": -1.25},
                            "field_meta": {"institution_holding_change": {
                                "source": "choice", "unit": "percentage_point", "period": "2026中报",
                                "caliber": "top10_shareholders", "period_basis": "YTD",
                                "quality": {"status": "single_source", "reason_codes": ["single_family"]},
                                "rule_eligible": True}}},
        }
        monkeypatch.setattr(explore_agents, "run_agent_loop", (
            lambda **kwargs: SimpleNamespace(success=False, content="", total_steps=0, error="模型请求超时")
        ))
        token = research_dimension.set("capital")
        try:
            with run.bind():
                result = explore_agents.run_capital_agent("600519", "贵州茅台", object())
        finally:
            research_dimension.reset(token)
        assert result["ok"] is False
        facts = {fact["label"]: fact for fact in run.facts["capital"]}
        # issue #64：单日窗口不再称「当日」——三窗口按实际名称留存
        assert {"总市值", "流通市值", "单日主力净流入", "5交易日主力净流入", "10交易日主力净流入"} <= set(facts)
        assert facts["总市值"]["value"] == "2050000000000.0"
        assert facts["总市值"]["unit"] == "currency_base / CNY"
        assert "single_source" in facts["总市值"]["qualification"]
        # 共享上下文的合格机构记录不复活为资金留存事实
        assert "机构持仓变化" not in facts

    def test_rejected_cap_record_retains_refusal_qualification(self, native_flow, monkeypatch):
        """拒绝记录留存不升级为可用数字：拒绝原因在扁平资格标注中可见。

        两类拒绝都不得与合格记录展示相同：带 normalization_error 的保留真实
        原因码；仅 rule_eligible=False 无其他原因文字的也必须标出已知不合格。
        """
        from src.core.research_run import research_dimension

        run = self._bound_run(monkeypatch)
        run.context.fundamental = {
            "market_cap": 2050000000000.0,
            "field_meta": {
                "total_mv": {"source": "choice", "value": 2050000000000.0,
                             "unit": "currency_base", "currency": "CNY",
                             "quality": {"status": "single_source"},
                             "rule_eligible": False},
                "circ_mv": {"source": "choice", "value": 2000000000000.0,
                            "unit": "currency_base", "currency": "CNY",
                            "rule_eligible": False, "normalization_error": "unit_conflict"},
            },
        }
        monkeypatch.setattr(explore_agents, "run_agent_loop", self._failing_model("timeout"))
        token = research_dimension.set("capital")
        try:
            with run.bind():
                explore_agents.run_capital_agent("600519", "贵州茅台", object())
        finally:
            research_dimension.reset(token)
        facts = {fact["label"]: fact for fact in run.facts["capital"]}
        assert facts["总市值"]["value"] == "2050000000000.0"  # 原值保留，不丢弃也不美化
        assert facts["总市值"]["qualification"] != facts["流通市值"]["qualification"]
        assert "资格不合格" in facts["总市值"]["qualification"]
        assert "unit_conflict" in facts["流通市值"]["qualification"]

    def test_cancelled_run_never_retains_late_facts(self, monkeypatch):
        """取消后护栏仍生效：迟到留存不写入（issue #56 既有护栏不回退）。"""
        run = ResearchRun(timeout_seconds=60.0)
        run.stop("cancelled")
        with run.bind():
            run.retain_facts("analyze_trend", {"ma20": 10.0})
            run.retain_facts("get_stock_info", {"total_mv": 1.0})
        assert run.facts == {}

    @staticmethod
    def _qualified_cap_meta() -> dict[str, Any]:
        return {
            "source": "frozen-choice", "value": 2050000000000.0,
            "unit": "currency_base", "currency": "CNY",
            "quality": {"status": "single_source"}, "rule_eligible": True,
        }
