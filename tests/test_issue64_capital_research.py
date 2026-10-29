# -*- coding: utf-8 -*-
"""Issue #64 资金研究员：机构/股东侧退役，完成条件只看合格资金三窗口。

规格：合格三个资金窗口在机构资料缺失/额度拒绝时仍正常分析；缺窗口/单位/身份
或模型失败仍真实降级；不得为退役字段再查询 iFinD 股东、公开股东/持仓表；
flow_summary/金额说明采用实际截至日，不得把单日写成报告当天。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.agent.deep_research import explore_agents
from src.agent.tools import data_tools
from src.deep_research.researchers.capital import parse

DATES: List[str] = [
    "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-26", "2026-10-23",
    "2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19", "2026-10-16",
    "2026-10-15", "2026-10-14",
]


@pytest.fixture
def flow_fixture(monkeypatch: pytest.MonkeyPatch):
    """合格 Choice 资金三窗口（T 完整）+ 机构侧零出站计数器。"""
    from data_provider.base import DataFetcherManager
    from data_provider.choice_adapter import ChoiceAdapter
    from src.config import Config

    monkeypatch.setattr(Config, "_instance", Config(data_source_profile="choice", deep_research_cross_validate=False))
    ordered = list(reversed(DATES))

    def request(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
        if method == "tradedates":
            return {"data": ordered}
        assert method == "csd" and args[1] == "NETINFLOW"
        values = [float(i + 1) for i in range(len(ordered))]
        return {"codes": [args[0]], "indicators": ["NETINFLOW"], "dates": ordered,
                "data": {args[0]: [values]}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(request))
    manager = DataFetcherManager()
    monkeypatch.setattr(data_tools, "_fetcher_manager_singleton", manager)

    calls: Dict[str, int] = {"fundamental_context": 0, "ifind_research": 0}

    def no_fundamental(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        calls["fundamental_context"] += 1
        raise AssertionError("机构块取数已退役，不得调用 get_fundamental_context")

    def no_ifind(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        calls["ifind_research"] += 1
        raise AssertionError("iFinD 股东/持仓查询已退役，不得调用")

    monkeypatch.setattr(data_tools._get_fetcher_manager(), "get_fundamental_context", no_fundamental)
    monkeypatch.setattr(
        data_tools._get_fetcher_manager(), "get_ifind_research_context", no_ifind, raising=False,
    )
    return calls


def _model_output(content: str, captured: List[str]):
    def generate(**kwargs: Any):
        captured.append(str(kwargs["messages"][-1]["content"]))
        return SimpleNamespace(success=True, content=content, total_steps=1, error=None)

    return generate


def test_capital_completes_on_flow_without_institution(flow_fixture, monkeypatch) -> None:
    """机构资料缺失/拒绝时，合格资金三窗口仍正常完成（ok，不降级）。"""
    captured: List[str] = []
    generate = _model_output(
        '{"flow_score": 82, "flow_summary": "主力资金持续净流入", '
        '"narrative": "近10个交易日主力净流入金额为正。"}', captured,
    )
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True, str(result)
    dim, _ = parse(result["data"], 1)
    assert dim.status == "ok", dim.degraded_reason
    assert dim.flow_score == 82
    assert not dim.institution_score, "机构评分退役，不得有值"
    assert not dim.institution_summary


def test_capital_research_never_fetches_institution(flow_fixture, monkeypatch) -> None:
    """资金研究员全程零机构/股东出站（含额度拒绝场景也零新增查询）。"""
    captured: List[str] = []
    generate = _model_output('{"flow_score": 60, "narrative": "观察。"}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True
    assert flow_fixture == {"fundamental_context": 0, "ifind_research": 0}


def test_capital_task_has_no_institution_output(flow_fixture, monkeypatch) -> None:
    """任务文本不再注入机构资料、不再要求机构评分或合成总分输出。"""
    captured: List[str] = []
    generate = _model_output('{"flow_score": 60, "narrative": "观察。"}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    explore_agents.run_capital_agent("300398", "飞凯材料", object())
    task = captured[-1]
    assert "institution_score" not in task
    assert "institution_summary" not in task
    assert "机构/股东资料" not in task
    assert "股东户数" not in task


def test_flow_summary_uses_actual_as_of_not_today(flow_fixture, monkeypatch) -> None:
    """flow_summary 携带实际截至日；不得用“当日/今日”冒充资金日期。"""
    captured: List[str] = []
    generate = _model_output('{"flow_score": 60, "narrative": "观察。"}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    dim, _ = parse(result["data"], 1)
    summary = dim.flow_summary
    assert "2026-10-29" in summary, summary
    assert "当日" not in summary and "今日" not in summary
    assert "万元" in summary and "未独立核验" in summary


def test_flow_window_gap_keeps_real_degradation(flow_fixture, monkeypatch) -> None:
    """资金窗口不合格时不启动模型，真实缺口保留（不补零、不伪装今日）。"""
    import data_provider.choice_adapter as ca_module

    def t_missing(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
        if method == "tradedates":
            return {"data": list(reversed(DATES))}
        # 只留 T-1 结束的 6 日 → 不满足回退条件
        kept = [d for d in reversed(DATES) if d != "2026-10-29"][:6]
        return {"codes": [args[0]], "indicators": ["NETINFLOW"], "dates": list(reversed(kept)),
                "data": {args[0]: [[float(i + 1) for i in range(len(kept))]]}}

    monkeypatch.setattr(ca_module.ChoiceAdapter, "_native_request", staticmethod(t_missing))
    called: List[Any] = []

    def generate(**kwargs: Any) -> Any:
        called.append(kwargs)
        return SimpleNamespace(success=True, content="{}", total_steps=1, error=None)

    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert called == [], "资金不合格不得启动模型"
    assert result["ok"] is False
    assert "窗口不完整" in result["error"] or "资金流" in result["error"]
    assert "机构" not in str(result.get("error")), "退役机构侧不再参与缺口说明"


def test_single_day_valid_with_window_hole_never_completes(flow_fixture, monkeypatch) -> None:
    """R5 反例：T 单日有效但 10 日序列中间缺日——三窗口未共同合格，不得 ok。"""
    import data_provider.choice_adapter as ca_module

    ordered = list(reversed(DATES))
    values = {day: float(index + 1) for index, day in enumerate(ordered)}
    values["2026-10-20"] = None  # 10 日窗口中间洞；T 单日有效

    def request(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
        if method == "tradedates":
            return {"data": ordered}
        assert method == "csd" and args[1] == "NETINFLOW"
        dates = [d for d in ordered if values.get(d) is not None]
        return {"codes": [args[0]], "indicators": ["NETINFLOW"], "dates": dates,
                "data": {args[0]: [[values[d] for d in dates]]}}

    monkeypatch.setattr(ca_module.ChoiceAdapter, "_native_request", staticmethod(request))
    called: List[Any] = []

    def generate(**kwargs: Any) -> Any:
        called.append(kwargs)
        return SimpleNamespace(success=True, content='{"flow_score": 80}', total_steps=1, error=None)

    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert called == [], "单日有效但窗口有洞时不得启动模型"
    assert result["ok"] is False


def test_qualified_flow_score_becomes_dim_score_discarding_composite(flow_fixture, monkeypatch) -> None:
    """三窗口合格时 dim.score = 合格 flow_score；模型旧合成总分一律丢弃。"""
    captured: List[str] = []
    generate = _model_output(
        '{"flow_score": 82, "score": 90, "narrative": "近10个交易日主力持续净流入。"}', captured,
    )
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True
    dim, _ = parse(result["data"], 1)
    assert dim.status == "ok"
    assert dim.flow_score == 82
    assert dim.score == 82, "资金总分应从合格 flow_score 派生，不得用旧合成值或保持 None"


def test_valid_zero_flow_score_preserved(flow_fixture, monkeypatch) -> None:
    """合法零分保留：flow_score=0 不得被当作缺失或降级。"""
    captured: List[str] = []
    generate = _model_output('{"flow_score": 0, "narrative": "资金面偏弱。"}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True
    dim, _ = parse(result["data"], 1)
    assert dim.flow_score == 0 and dim.score == 0
    assert dim.status == "ok"


def test_model_output_missing_score_degrades(flow_fixture, monkeypatch) -> None:
    """模型成功但未给出可用 flow_score：不得仅凭固定 summary 变 ok。"""
    captured: List[str] = []
    generate = _model_output('{"flow_summary": "主力净流入", "narrative": "观察。"}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True, "模型调用本身成功"
    dim, _ = parse(result["data"], 1)
    assert dim.status == "degraded", dim.degraded_reason
    assert dim.score is None and dim.flow_score is None
    assert dim.flow_summary, "已取得的资金事实保留在固定 summary"


def test_nested_flow_score_adapted_via_existing_boundary(flow_fixture, monkeypatch) -> None:
    """R11 反例：嵌套 flow_score={"score": 80} + 有效 narrative → 沿既有适配边界
    归一为 80，不因 raw 形状误降级；资金总分 = 80。"""
    captured: List[str] = []
    generate = _model_output(
        '{"flow_score": {"score": 80}, "narrative": "近10个交易日主力持续净流入。"}', captured,
    )
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True
    dim, _ = parse(result["data"], 1)
    assert dim.status == "ok", dim.degraded_reason
    assert dim.flow_score == 80 and dim.score == 80


def test_bool_flow_score_degrades(flow_fixture, monkeypatch) -> None:
    """布尔/非数字字符串分不构成有效评分：degraded；原始值保留为诊断事实。"""
    captured: List[str] = []
    generate = _model_output('{"flow_score": true, "narrative": "观察结论段落。"}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    dim, _ = parse(result["data"], 1)
    assert dim.status == "degraded" and dim.score is None


def test_model_score_only_without_analysis_degrades(flow_fixture, monkeypatch) -> None:
    """R5 反例：仅 {"flow_score": 80} 无任何分析正文——固定 summary 不替代模型分析。"""
    captured: List[str] = []
    generate = _model_output('{"flow_score": 80}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True
    dim, _ = parse(result["data"], 1)
    assert dim.status == "degraded", dim.degraded_reason
    # 模型原始评分保留为事实，但不得成为 dim.score（无分析正文不构成有效结论）
    assert dim.score is None and dim.flow_score == 80
    assert dim.flow_summary, "已取得的资金事实保留在固定 summary"


# ---------------------------------------------------------------------------
# 首份真实报告（300398_202610292345）失败反例的修复回归：退役字段在生产者
# 边界整键缺席、打包 SKILL 现行规则真实出库、适配边界与契约层评分口径收敛。
# ---------------------------------------------------------------------------


def test_deep_capital_block_omits_retired_sector_key(flow_fixture, monkeypatch) -> None:
    """include_sector_flows=False 时源块整键无 sector_rankings（真实报告反例：
    源块构造 sector_rankings={} 空占位注入模型输入）。True 消费者不受影响。"""
    from src.agent.tools.data_tools import _capital_flow_tool_result

    manager = data_tools._get_fetcher_manager()

    def fake_sector(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {
            "sector_rankings": {"top": [{"name": "半导体", "inflow": 5e8}], "bottom": []},
            "errors": [],
        }

    monkeypatch.setattr(manager._fundamental_adapter, "get_capital_flow", fake_sector)
    deep_block = manager.get_capital_flow_context("300398", include_sector_flows=False)
    assert "sector_rankings" not in deep_block["data"], "退役字段不得以空占位构造进源块"
    deep_result = _capital_flow_tool_result("300398", deep_block)
    assert "sector_rankings" not in deep_result, "共享投影不得为退役字段合成空榜单"
    # 正常 include_sector_flows=True 公开消费者契约不变：字段存在且照常投影
    true_block = manager.get_capital_flow_context("300398", include_sector_flows=True)
    assert true_block["data"]["sector_rankings"] == {
        "top": [{"name": "半导体", "inflow": 5e8}], "bottom": [],
    }
    true_result = _capital_flow_tool_result("300398", true_block)
    assert true_result["sector_rankings"]["top_inflow_sectors"] == [{"name": "半导体", "inflow": 5e8}]


def test_qualified_quality_record_survives_capital_projection(flow_fixture, monkeypatch) -> None:
    """真实验收反例（在途审查发现）：源记录既有质量口径随共享投影原样保留。

    provider 构造的 main_inflow 资格记录带 quality=single_source/single_family；
    投影边界此前经 adopted_field_record 缺省把非冲突质量重置为
    unverified/空 reasons（实际捕获任务与留存事实均为 unverified）。否决记录
    （normalization_error）的拒绝语义不得因质量透传放宽。
    """
    from src.agent.tools.data_tools import _capital_flow_tool_result

    manager = data_tools._get_fetcher_manager()
    block = manager.get_capital_flow_context("300398", include_sector_flows=False)
    source_record = block["data"]["stock_flow"]["field_meta"]["main_inflow"]
    assert source_record["quality"] == {"status": "single_source", "reason_codes": ["single_family"]}
    result = _capital_flow_tool_result("300398", block)
    projected = result["field_meta"]["main_inflow"]
    assert projected["quality"] == source_record["quality"], "既有合格质量口径不得被投影重置"
    assert projected["rule_eligible"] is True

    # 否决记录保持否决：normalization_error 记录不因质量处理变成合格
    vetoed_block = {
        "status": "ok",
        "data": {"stock_flow": {
            "main_net_inflow": 1.0,
            "field_meta": {"main_inflow": {
                "field": "main_inflow", "value": 1.0, "unit": None,
                "normalization_error": "unit_conflict", "rule_eligible": False,
                "quality": {"status": "missing", "reason_codes": ["window_incomplete"]},
            }},
        }},
        "errors": [],
    }
    vetoed = _capital_flow_tool_result("300398", vetoed_block)
    vetoed_record = vetoed["field_meta"]["main_inflow"]
    assert vetoed_record["rule_eligible"] is False, "元数据否决记录不得变合格"
    assert vetoed_record.get("normalization_error") == "unit_conflict"

    # 冲突证据原样保留：同值 conflict 记录的 status 与拒绝 reasons 都不得被抹掉
    # （交叉验证负证据是拒绝理由，不是可丢弃状态标签）。
    conflict_block = {
        "status": "ok",
        "data": {"stock_flow": {
            "main_net_inflow": 1.0,
            "field_meta": {"main_inflow": {
                "field": "main_inflow", "value": 1.0, "unit": "currency_base",
                "rule_eligible": False,
                "quality": {"status": "conflict", "reason_codes": ["value_conflict"]},
            }},
        }},
        "errors": [],
    }
    conflict = _capital_flow_tool_result("300398", conflict_block)
    conflict_record = conflict["field_meta"]["main_inflow"]
    assert conflict_record["quality"] == {"status": "conflict", "reason_codes": ["value_conflict"]}, \
        "同值冲突记录的拒绝 reasons 不得被投影抹掉"
    assert conflict_record["rule_eligible"] is False, "冲突记录保持否决"


def test_missing_packaged_capital_rules_fail_before_model_or_fetch(flow_fixture, monkeypatch) -> None:
    """打包 capital 现行规则缺失/为空时显式失败（对齐既有 business 守卫）：
    不做供应商备料、不启动模型，不静默退回「宪法+试点边界」空规则发请求。"""
    import data_provider.choice_adapter as ca_module
    from src.deep_research_dims import narrate

    requests: List[str] = []
    original_request = ca_module.ChoiceAdapter._native_request

    def counting_request(method: str, args: Any, options: Any, deadline: Any) -> Dict[str, Any]:
        requests.append(method)
        return original_request(method, args, options, deadline)

    monkeypatch.setattr(ca_module.ChoiceAdapter, "_native_request", staticmethod(counting_request))
    monkeypatch.setattr(
        explore_agents, "load_dim_prompt",
        lambda dim_id: "" if dim_id == "capital" else narrate.load_dim_prompt(dim_id),
    )
    called: List[Any] = []

    def generate(**kwargs: Any) -> Any:
        called.append(kwargs)
        return SimpleNamespace(success=True, content="{}", total_steps=1, error=None)

    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is False
    assert "提示词" in str(result.get("error"))
    assert called == [], "现行规则缺失不得启动模型"
    assert requests == [], "现行规则缺失不得做资金取数（供应商备料前显式失败）"


def test_legacy_capital_block_converges_on_retired_sector_key(flow_fixture, monkeypatch) -> None:
    """legacy 路径 include_sector_flows=False 同样整键缺席（与 Choice 分支收敛）。"""
    from src.config import Config

    monkeypatch.setattr(Config._instance, "data_source_profile", "legacy")
    manager = data_tools._get_fetcher_manager()

    def adapter_payload(stock_code: str, include_sector: bool = True) -> Dict[str, Any]:
        rankings = (
            {"top": [{"name": "白酒"}], "bottom": [{"name": "煤炭"}]}
            if include_sector
            else {"top": [], "bottom": []}
        )
        return {
            "status": "ok",
            "stock_flow": {"main_net_inflow": 1.0, "inflow_5d": 2.0, "inflow_10d": 3.0},
            "sector_rankings": rankings,
            "source_chain": [],
            "errors": [],
        }

    monkeypatch.setattr(manager._fundamental_adapter, "get_capital_flow", adapter_payload)
    deep_block = manager.get_capital_flow_context("600519", include_sector_flows=False)
    assert "sector_rankings" not in deep_block["data"]
    true_block = manager.get_capital_flow_context("600519", include_sector_flows=True)
    assert "sector_rankings" in true_block["data"]
    assert true_block["data"]["sector_rankings"]["top"] == [{"name": "白酒"}]


def test_capital_task_prompt_has_no_retired_sector_rankings(flow_fixture, monkeypatch) -> None:
    """实际模型输入（user task JSON）整键无 sector_rankings——飞凯实际失败反例。"""
    captured: List[str] = []
    generate = _model_output('{"flow_score": 60, "narrative": "观察。"}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True, str(result)
    task = captured[-1]
    assert '"sector_rankings"' not in task, "退役行业榜字段不得再进入实际模型输入"
    assert "top_inflow_sectors" not in task
    assert "top_outflow_sectors" not in task


def test_capital_system_prompt_ships_module_skill_rules(flow_fixture, monkeypatch) -> None:
    """无本地 capital 覆盖时，打包模块 SKILL 现行规则真实进入系统消息。

    真实报告观测：系统消息只有宪法+试点边界、无任何资金现行规则（退役边界、
    flow_score 口径），实际报告叙述中出现不应有的「行业资金排名缺失」缺口。
    frontmatter 不注入。
    """
    captured: List[Dict[str, Any]] = []

    def generate(**kwargs: Any) -> Any:
        captured.append(dict(messages=list(kwargs["messages"])))
        return SimpleNamespace(success=True, content='{"flow_score": 60, "narrative": "观察。"}', total_steps=1, error=None)

    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True, str(result)
    system = str(captured[0]["messages"][0]["content"])
    assert "共享宪法" in system and "本轮职责边界" in system
    assert "资金总分" in system, "打包 SKILL 的资金总分口径必须真实出库"
    assert "机构/股东侧" in system and "退役" in system, "现行退役边界必须真实出库"
    assert "name: researcher_capital" not in system, "SKILL frontmatter 不注入 prompt"


def test_stale_local_capital_override_cannot_replace_current_scope(
    flow_fixture, monkeypatch, tmp_path
) -> None:
    """部署遗留退役期本地覆盖不得替换强制现行范围（不得复活机构/行业榜要求）。"""
    import os
    import shutil

    from src.deep_research_dims import narrate

    shutil.copy(os.path.join(narrate._AGENTS_DIR, "_constitution.md"), tmp_path / "_constitution.md")
    (tmp_path / "capital.md").write_text(
        "必须输出 institution_score 与行业资金排名 sector_rankings。", encoding="utf-8"
    )
    monkeypatch.setattr(narrate, "_AGENTS_DIR", str(tmp_path))
    assert "institution_score" not in narrate.load_dim_prompt("capital")
    captured: List[Dict[str, Any]] = []

    def generate(**kwargs: Any) -> Any:
        captured.append(dict(messages=list(kwargs["messages"])))
        return SimpleNamespace(success=True, content='{"flow_score": 60, "narrative": "观察。"}', total_steps=1, error=None)

    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True, str(result)
    system = str(captured[0]["messages"][0]["content"])
    assert "institution_score" not in system
    assert "sector_rankings" not in system
    assert "资金总分" in system, "强制现行范围不因遗留覆盖缺席"


def test_capital_dim_contract_accepts_numeric_string_today() -> None:
    """离线复现既有契约层行为：CapitalDim lax float 接受数字字符串。"""
    from src.schemas.deep_research_dims import CapitalDim

    assert CapitalDim(dim="capital", flow_score="82").flow_score == 82.0


def test_numeric_string_flow_score_not_degraded_by_boundary(flow_fixture, monkeypatch) -> None:
    """适配边界与契约层同口径：数字字符串评分不再被边界误降级。"""
    captured: List[str] = []
    generate = _model_output(
        '{"flow_score": "82", "narrative": "近10个交易日主力持续净流入。"}', captured,
    )
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True
    dim, _ = parse(result["data"], 1)
    assert dim.status == "ok", dim.degraded_reason
    assert dim.flow_score == 82 and dim.score == 82


def test_non_numeric_string_flow_score_still_degrades(flow_fixture, monkeypatch) -> None:
    """非数字字符串不构成评分：边界真实降级；契约层拒绝既有路径不变（不静默吞掉）。"""
    from pydantic import ValidationError

    captured: List[str] = []
    generate = _model_output('{"flow_score": "abc", "narrative": "观察。"}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True
    assert result["data"]["status"] == "degraded"
    assert "score" not in result["data"], "无效字符串评分不得派生资金总分"
    with pytest.raises(ValidationError):
        parse(result["data"], 1)


def test_non_finite_string_flow_score_degrades(flow_fixture, monkeypatch) -> None:
    """NaN/Inf 字符串不归一为有效评分（float 可解析但非有限）：真实降级不冒充数值。"""
    from pydantic import ValidationError

    captured: List[str] = []
    generate = _model_output('{"flow_score": "NaN", "narrative": "观察。"}', captured)
    monkeypatch.setattr(explore_agents, "run_agent_loop", generate)
    result = explore_agents.run_capital_agent("300398", "飞凯材料", object())
    assert result["ok"] is True
    assert result["data"]["status"] == "degraded"
    assert "score" not in result["data"], "非有限字符串评分不得派生资金总分"
    with pytest.raises(ValidationError):
        parse(result["data"], 1)
