# -*- coding: utf-8 -*-
"""探索型维度 Agent：S3 情报 / L4 产业链（唯二需要 ReAct 工具循环的维度）。

工具子集精选（factory.py 模式），max_steps=8；输出强制 JSON，契约校验失败重试 1 次。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date
from functools import partial
from time import monotonic
from typing import Any, Callable, Dict, Optional

from src.agent.runner import run_agent_loop
from src.agent.tools.registry import ToolRegistry
from src.deep_research_dims.narrate import load_constitution, load_dim_prompt

logger = logging.getLogger(__name__)

from src.config import get_config
from src.core.research_run import current_research

_INTEL_TOOLS = {
    "search_stock_news",
    "search_comprehensive_intel",
    "get_market_indices",
}
_SUPPLY_CHAIN_TOOLS = {
    "search_comprehensive_intel",
    "verify_supply_chain_evidence",
    "get_sector_rankings",
    "get_stock_info",
}


def _build_registry(source_registry: Any, names: set[str]) -> Any:
    """从问股注册表复制工具子集到独立实例（不污染问股单例）。"""
    registry = ToolRegistry()
    for tool in source_registry.list_tools():
        if tool.name in names:
            registry.register(tool)
    if "verify_supply_chain_evidence" in names and registry.get("verify_supply_chain_evidence") is None:
        # This verifier uses public Eastmoney/THS pages, not retired iFinD/MCP APIs.
        from src.agent.tools.supply_chain_tools import verify_supply_chain_evidence_tool
        registry.register(verify_supply_chain_evidence_tool)
    missing = names - set(registry.list_names())
    if missing:
        logger.warning("[DualTrack] 探索维度缺失工具: %s", sorted(missing))
    return registry


def _registry_source() -> Any:
    from src.agent.factory import get_tool_registry

    return get_tool_registry()


def _run_explore(
    dim_id: str,
    tool_names: set[str],
    user_task: str,
    llm_adapter: Any,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_steps: int = 8,
    retry: int = 1,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """在共同截止时间内研究；仅成功响应的 JSON 格式错误允许修复。"""
    run = current_research.get()
    if run is not None:
        reserve = min(180.0, run.timeout_seconds * 0.15)
        deadline = min(deadline, run.deadline - reserve) if deadline is not None else run.deadline - reserve
    else:
        deadline = deadline if deadline is not None else monotonic() + 480.0
    prompt = load_dim_prompt(dim_id)
    if dim_id == "business" and not prompt.strip():
        return {"ok": False, "error": "业务画像提示词缺失，无法执行研究"}
    system = (
        ("" if dim_id == "business" else load_constitution())
        + "\n\n"
        + prompt
        + "\n\n最终回答必须是且仅是一个 JSON 对象（不要 Markdown 代码围栏外的任何文字）。"
    )
    if dim_id == "business":
        system += f"\n分析日期：{date.today().isoformat()}；日期未知的资料不得称为最新。"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_task},
    ]
    registry = _build_registry(_registry_source(), tool_names)
    if dim_id == "business":
        tool = registry.get("search_comprehensive_intel")
        if tool is not None:
            registry.register(replace(tool, handler=partial(tool.handler, profile="business"),
                                      description="Search company business, products and competitors; returns evidence with URLs and dates."))

    # Reserve two thirds for the substantive response; never reset on JSON repair.
    research_deadline = monotonic() + max(0.0, deadline - monotonic()) * (1 / 3 if dim_id == "business" else 2 / 3)
    last_error = ""
    for attempt in range(retry + 1):
        if attempt and research_deadline is not None:
            research_deadline = min(research_deadline, monotonic())
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {"ok": False, "error": "研究时限耗尽"}
        loop = run_agent_loop(
            messages=messages,
            tool_registry=registry,
            llm_adapter=llm_adapter,
            max_steps=min(max_steps, 2) if dim_id == "business" else max_steps,
            **({"research_deadline": research_deadline} if research_deadline is not None else {}),
            progress_callback=progress_callback,
            max_wall_clock_seconds=remaining,
            stock_scope=None,
        )
        # Only a successful response with malformed JSON warrants format repair.
        if not loop.success:
            original = loop.error or "模型调用未完成"
            lowered = original.lower()
            if run is not None and run.stop_reason:
                error = run.reason_text
            elif "等待模型额度" in original:
                error = "等待模型额度超时，尚未发送模型请求"
            elif "Agent timed out" in original or "insufficient budget" in lowered:
                error = "本维度研究预算不足，保留已取得资料"
            elif "timeout" in lowered or "timed out" in lowered:
                error = "模型请求超时，未完成本维度研究"
            elif "max steps" in lowered:
                error = "研究步数已用完"
            elif "length" in lowered:
                error = "模型输出被截断（length）"
            elif "content_filter" in lowered:
                error = "模型输出被服务商过滤（content_filter）"
            else:
                error = "模型调用失败，未完成本维度研究"
            return {"ok": False, "error": error, "steps": loop.total_steps}
        parsed = _extract_json(loop.content)
        if parsed:
            return {"ok": True, "data": parsed, "steps": loop.total_steps}
        last_error = loop.error or "LLM 未输出可解析 JSON"
        messages.append(
            {
                "role": "user",
                "content": "你的最终回答不是合法 JSON。请重新输出：仅一个 JSON 对象，无其他文字。",
            }
        )
    return {"ok": False, "error": last_error}


def _extract_json(content: str) -> Optional[Dict[str, Any]]:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        import json

        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    try:
        import json_repair  # type: ignore[import-not-found]

        obj = json_repair.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


def run_intel_agent(
    stock_code: str,
    stock_name: str,
    llm_adapter: Any,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_steps: int = 8,
) -> Dict[str, Any]:
    """S3 情报探索：新闻/公告/风险/催化剂 + 贝叶斯证据候选。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）执行情报收集并直接输出 JSON：\n"
        "1) 先用 search_stock_news 查个股新闻公告，search_comprehensive_intel 查宏观/行业情报，"
        "get_market_indices 取大盘环境；\n"
        "2) 输出 JSON 字段：sentiment_summary(字符串)、earnings_outlook(字符串)、"
        "risk_alerts(字符串数组)、positive_catalysts(字符串数组)、latest_news(字符串)、"
        "unverified_count(整数)、"
        "evidence_items(数组，元素：{evidence,strength,lr,date}；strength 取 "
        "strong_positive/weak_positive/neutral/weak_negative/strong_negative 之一，"
        "lr 为该证据似然比(正数)，date 为 YYYY-MM-DD；只收录近 90 天证据，最多 6 条)；\n"
        "3) root_cause(对象：{event, mechanism, magnitude, persistence})——"
        "当前盘面结构(涨/跌/震荡)的根因：发生了什么事件、通过什么机制影响股价、"
        "影响量级(正面利好/负面利空/有限)、预计持续性；基于你搜到的情报作答，搜不到就留空；\n"
        "4) event_calendar(数组，最多 6 项：{event, date, outcomes(2-4个可能结果), "
        "plans(每个结果对应的一条交易预案)})——只收录当前时点之后尚未发生的事件"
        "(财报/解禁/政策/行业会议等)，禁止列出已过去年份的事件；每个事件给出可能结果和对应预案；"
        "不确定日期写\"未知\"。\n"
        "事件预案编号联动：plans 每条预案开头引用操作编号——T1-L 左侧买入 ｜ T1-R 右侧确认买入 ｜ "
        "T1-S1 减仓/止盈 ｜ T1-SL 止损。会导致无论价格无条件离场的事件，在预案末尾标注「一票否决」。\n"
        "来源等级标注（primary/news/industry/community_*/inferred/unverified）写在字符串内容里。"
    )
    return _run_explore(
        "intel", _INTEL_TOOLS, task, llm_adapter, progress_callback, max_steps
    )


def run_supply_chain_agent(
    stock_code: str,
    stock_name: str,
    llm_adapter: Any,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_steps: int = 8,
) -> Dict[str, Any]:
    """L4 产业链探索：图谱/定位/瓶颈 + 双源校验。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）执行产业链调研并直接输出 JSON：\n"
        "1) 用 search_comprehensive_intel 调研产业链位置与瓶颈，get_sector_rankings 看板块归属，"
        "verify_supply_chain_evidence 做东财+同花顺双源校验，get_stock_info 取基本面佐证；\n"
        "2) 输出 JSON 字段：company_position(字符串，必填：公司在产业链中的环节定位)、"
        "chain_map(数组，元素：{level(上/中/下游之一), companies(字符串数组), concentration(可选字符串)})、"
        "chokepoints(数组，元素：{type(取 patent/capacity/geo/tech/cert 之一), description(字符串), "
        "confidence(取 high/medium/low)})、upstream(字符串数组)、downstream(字符串数组)、"
        "bargaining_power(字符串)、us_china_chain(对象：{role, substitution_progress, sanction_risk, "
        "dual_chain_impact}，低相关行业可省略该对象)、"
        "verification_status(取 confirmed/partial/conflict/unverified/not_applicable 之一)。\n"
        "所有具体客户/份额/产能数字必须带来源等级标签；取不到就写 inferred 并说明依据。"
    )
    return _run_explore(
        "supply_chain",
        _SUPPLY_CHAIN_TOOLS,
        task,
        llm_adapter,
        progress_callback,
        max_steps,
    )


# ---------------------------------------------------------------------------
# 八大研究员 · 新增 5 员（方案 v3：基金经理-研究员架构）
# ---------------------------------------------------------------------------

_TECHNICAL_TOOLS = {"get_daily_history", "analyze_trend"}
_CAPITAL_TOOLS = {"get_capital_flow", "get_chip_distribution", "get_stock_info"}
_SENTIMENT_TOOLS = {"search_stock_news", "search_market_discussion"}
_OWNERSHIP_TOOLS = {
    "search_comprehensive_intel",
    "search_stock_news",
    "search_knowledge_base",
}
_US_CHINA_TOOLS = {"search_comprehensive_intel", "verify_supply_chain_evidence"}


def _chanlun_engine_facts(stock_code: str) -> str:
    """P0-1：缠论引擎真实结构（优先数据源）。引擎不可用时返回空串，LLM 退回工具取数。"""
    if run := current_research.get():
        run.check()
    try:
        import pandas as pd

        from chanlun.chanlun_engine import ChanLunEngine
        from src.services.history_loader import load_history_df

        df, _src = load_history_df(stock_code, days=250)
        if df is None or df.empty or len(df) < 60:
            return ""
        df = df.copy()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        out = ChanLunEngine(df).analyze()
        return (
            f"【缠论引擎结构（权威，禁止矛盾）】趋势={out.get('current_trend')}"
            f"，中枢位置={out.get('position')}，背驰={out.get('divergence') or '无'}；"
            f"笔数={len(out.get('strokes') or [])}，中枢数={len(out.get('zhongshus') or [])}。"
        )
    except Exception:  # noqa: BLE001 - 引擎失败不阻断研究员
        return ""


def run_technical_agent(stock_code, stock_name, llm_adapter, progress_callback=None, max_steps=8):
    """技术研究员：缠论引擎结构注入（LLM 只解读），+ 支撑压力 + MACD/RSI/波浪。"""
    engine_facts = _chanlun_engine_facts(stock_code)
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做技术面深度分析并直接输出 JSON：\n"
        f"0) 缠论结构事实（引擎已算好，你的解读必须与之一致，禁止矛盾）：{engine_facts or '引擎不可用，用工具取数后自行判定'}\n"
        "1) 用 get_daily_history 取 250 天日线，analyze_trend 取综合指标；\n"
        "2) 输出 JSON：chanlun_summary(字符串：基于引擎结构的解读——为什么是这个趋势/位置/背驰)、"
        "support(数值)、resistance(数值)、indicator_summary(字符串：MACD/RSI/均线状态)、"
        "wave_note(字符串：波浪理论视角的一句判断，不适用则写不适用)、"
        "score(0-100 综合技术分，须与引擎结构方向一致)、basis(打分依据一句话，含具体数字)、"
        "narrative(50-100 字结论：必须含至少 2 个硬数字——价位/百分比/指标值，禁止只给分无论据)。"
    )
    return _run_explore("technical", _TECHNICAL_TOOLS, task, llm_adapter, progress_callback, max_steps)


def run_capital_agent(stock_code: str, stock_name: str, llm_adapter: Any, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None, max_steps: int = 8) -> Dict[str, Any]:
    """资金研究员：资金流向 + 机构/大户持仓变动 + 筹码成本结构。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做资金面深度分析并直接输出 JSON：\n"
        "1) get_capital_flow 取主力资金流（当日/5日/10日），get_chip_distribution 取筹码结构，"
        "get_stock_info 取机构持仓与基本面佐证；\n"
        "2) 输出 JSON：flow_summary、flow_score(0-100)、institution_summary"
        "（机构/大户持仓变动解读）、institution_score(0-100)、chip_summary"
        "（筹码成本结构：获利盘/集中度/平均成本解读）、chip_score(0-100)、"
        "score(三项加权总分)、每项说明含具体数字；取不到的项 score 填 null；"
        "narrative(50-100 字结论：必须含至少 2 个硬数字——净流入金额/获利盘比例/成本价等，禁止只给分无论据)。"
    )
    tools = _CAPITAL_TOOLS
    flow = None
    institution_eligible = True
    if get_config().data_source_profile == "choice":
        import json
        from src.agent.tools.data_tools import _handle_get_capital_flow, _handle_get_stock_info
        deadline = monotonic() + get_config().fundamental_stage_timeout_seconds
        if run := current_research.get():
            run.check()
        flow = _handle_get_capital_flow(stock_code)
        if run := current_research.get():
            run.retain("get_capital_flow", flow)
        if run := current_research.get():
            run.check()
        info = _handle_get_stock_info(stock_code, deadline=deadline)
        if run := current_research.get():
            run.retain("get_stock_info", info)
        institution = (info.get("fundamental_context") or {}).get("institution") or {}
        institution_eligible = any(record.get("rule_eligible") for record in (institution.get("field_meta") or {}).values())
        tools = tools - {"get_capital_flow", "get_stock_info"}
        task += "\n资金流工具已执行，结果：" + json.dumps(flow, ensure_ascii=False)
        task += "\n基本面工具已执行，结果：" + json.dumps(info, ensure_ascii=False)
        task += "\n金额缺失或单位未确认时，flow_score、资金总分填 null，不推断资金金额、方向或交易行动。narrative 只说明已验证内容与缺口，不要求凑齐数字。"
        task += "\n机构与股东资料按各自 source_chain 引用；股东户数不等于持仓量，缺统计对象、日期或单位时只作线索，不给机构分数或方向结论。"
    out = _run_explore("capital", tools, task, llm_adapter, progress_callback, max_steps)
    if out.get("ok") and not institution_eligible:
        out["data"]["institution_score"] = None
        out["data"]["score"] = None
        out["data"]["institution_summary"] = "公开机构/股东资料仅作线索；统计对象、日期或单位未核实，不能据此判定持仓方向。"
    if flow is not None and out.get("ok") and flow.get("main_net_inflow") is not None:
        amounts = [f"{label} {flow[key] / 10000:,.2f} 万元" if flow.get(key) is not None else f"{label}窗口不完整" for key, label in (("main_net_inflow", "当日"), ("inflow_5d", "5交易日"), ("inflow_10d", "10交易日"))]
        out["data"]["flow_summary"] = f"Choice 主力净流入，截至{flow.get('expected_date')}已收盘交易日：" + "；".join(amounts) + "。单源数据，未独立核验。"
    if flow is not None and out.get("ok") and flow.get("main_net_inflow") is None:
        data = out["data"]
        data["flow_score"] = data["score"] = None
        data["flow_summary"] = flow.get("note") or "资金流数据不可用"
        data["narrative"] = data["flow_summary"]
        data["status"] = "degraded"
        data["degraded_reason"] = "; ".join(flow.get("errors") or [])
    return out


def run_sentiment_agent(stock_code, stock_name, llm_adapter, progress_callback=None, max_steps=8):
    """情绪研究员：机构评价 + 社区评价（雪球/微博/股吧，强制来源等级）。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做情绪面深度分析并直接输出 JSON：\n"
        "1) search_stock_news 查机构研报/评级观点，search_market_discussion 查雪球/微博/股吧讨论；\n"
        "2) 输出 JSON：institute_view(机构评价分层：评级/目标价/分歧点)、institute_score(0-100)、"
        "community_view(社区情绪：看多/看空比例与典型观点，带来源等级标签)、"
        "community_score(0-100)、unverified_count(社区传闻条数)、"
        "score(加权总分)、narrative(50-100 字结论：必须含至少 1 个硬数字——机构数/目标价家数/多空比例，"
        "取不到数字就如实写「缺 XX 数据」，禁止只给分无论据)；社区信息只作分歧线索，不得写成确认。"
    )
    return _run_explore("sentiment", _SENTIMENT_TOOLS, task, llm_adapter, progress_callback, max_steps)


def run_ownership_agent(stock_code: str, stock_name: str, llm_adapter: Any, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None, max_steps: int = 8, *, deadline: Optional[float] = None, research: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """股权高管研究员：实控人/十大股东/高管背景与变动（审计 A2：评分只作参考，不硬塞六维）。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做股权架构与高管背景调查并直接输出 JSON：\n"
        "1) search_knowledge_base 查已有资料，search_comprehensive_intel / search_stock_news "
        "查实控人/股东/高管的公开信息（结构化资料优先，搜索为补）；\n"
        "2) 输出 JSON：controller(实际控制人/控股股东，一句话)、top_holders(前十大股东要点数组)、"
        "executives(核心高管：姓名/职务/背景一句，数组)、recent_changes(近一年增减持/任免/股权变动数组)、"
        "data_gaps(取不到的数据项数组，禁止编造)、score(治理结构健康度参考分 0-100，可 null)、"
        "narrative(调查结论一段)。三个数组每条必须是对象（name 必填，附 source 来源标签，"
        "detail 补充说明，变动条目尽量附 date），不要输出 JSON 字符串或 {items:[…]} 包装；"
        "某列表无资料时输出空数组（空数组不代表没有发生变动），不得编造条目、日期或来源。所有事实带来源等级标签。"
    )
    if get_config().data_source_profile == "choice":
        import json
        from src.agent.tools.data_tools import _handle_get_stock_info
        if research is None:
            if run := current_research.get():
                run.check()
            info = _handle_get_stock_info(stock_code, deadline=deadline)
            research = (info.get("fundamental_context") or {}).get("research") or {}
        research = research or {}
        if run := current_research.get():
            run.retain_vendor("ownership", research)
        if get_config().enable_ifind_mcp and not research.get("shareholder_details"):
            from src.agent.tools.data_tools import _get_fetcher_manager
            from data_provider.ifind_fundamental_adapter import confirmed_disclosed_period
            if run := current_research.get():
                run.check()
            run_ctx = getattr(run, "context", None) if run else None
            fundamental = getattr(run_ctx, "fundamental", {}) or {}
            as_of = str(research.get("as_of") or getattr(run_ctx, "as_of", "") or "")
            period = confirmed_disclosed_period(research, fundamental, as_of=as_of)
            research = {**research, "shareholder_details": _get_fetcher_manager().get_ifind_research_context(stock_code, "holders", period=period, deadline=deadline)}
        task += "\n供应商公司资料（各字段来源以载荷标注为准，非独立双源核验）：" + json.dumps(research, ensure_ascii=False)
    research = research or {}
    details = research.get("shareholder_details")
    if run := current_research.get():
        run.retain_vendor("ownership", research)
    out = _run_explore("ownership", _OWNERSHIP_TOOLS, task, llm_adapter, progress_callback, max_steps, deadline=deadline)
    if not out.get("ok") and ((research.get("company") or {}).get("AHOLDER") or (research.get("holders") or {}).get("HOLDNAMER") or (details or {}).get("items")):
        out = {**out, "ok": True, "data": {"status": "degraded", "degraded_reason": str(out.get("error") or "研究模型未完成") + "；保留已取得股权资料", "data_gaps": ["研究模型未完成，高管与变动资料未核实"], "narrative": "以下为供应商结构化资料；模型研究未完成，不生成治理评分。"}}
    if out.get("ok") and research:
        company = research.get("company") or {}
        if company.get("AHOLDER"):
            out["data"]["controller"] = f"{company['AHOLDER']}（Choice，截至 {research.get('as_of')}）"
        holders = research.get("holders") or {}
        names = holders.get("HOLDNAMER")
        if isinstance(names, str) and names.strip():
            out["data"]["top_holders"] = [{"name": name.strip(), "source": f"Choice，报告期 {holders.get('period')}"} for name in names.split(";") if name.strip()]
    if out.get("ok"):
        out["data"].pop("shareholder_details", None)
        if details:
            out["data"]["shareholder_details"] = details
            if details.get("errors") or details.get("coverage") != "complete":
                out["data"]["data_gaps"] = list(dict.fromkeys([*(out["data"].get("data_gaps") or []), "iFinD股东资料覆盖不完整", *(details.get("errors") or [])]))
    out["vendor_research"] = research
    return out


def run_us_china_agent(stock_code, stock_name, llm_adapter, progress_callback=None, max_steps=8):
    """中美竞争研究员：从供应链 Agent 拆出（方案决策 1）。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做中美产业链竞争分析并直接输出 JSON：\n"
        "1) search_comprehensive_intel 调研出口管制/制裁/平行链影响，"
        "verify_supply_chain_evidence 校验板块归属；\n"
        "2) 输出 JSON：applicability(强相关/低相关/不适用+一句理由)、role_cn(中国链位置)、"
        "role_us(美国/全球链位置)、export_control(出口管制影响)、sanction_risk(制裁风险)、"
        "substitution(国产替代/平行替换进度与阶段标签)、"
        "score(中美链维度参考分 0-100，可 null)、narrative(一段结论)；"
        "低相关行业 applicability 写低相关，其余字段留空。"
    )
    return _run_explore("us_china", _US_CHINA_TOOLS, task, llm_adapter, progress_callback, max_steps)


_BUSINESS_TOOLS = {"search_comprehensive_intel"}


def run_business_agent(
    stock_code: str, stock_name: str, llm_adapter: Any,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_steps: int = 8, *, deadline: Optional[float] = None, research: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """基本面研究员（业务画像）：经营模式/主营产品/竞争地位/与龙头对比。"""
    if not load_dim_prompt("business").strip():
        return {"ok": False, "error": "业务画像提示词缺失，无法执行研究"}
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做业务画像与竞争地位分析并直接输出 JSON：\n"
        "1) 用 search_comprehensive_intel 一次调研主营业务、产品和竞争地位；资料不足标缺失，不重复搜索；\n"
        "2) 输出 JSON：business_model(一句话经营模式)、main_products(主营产品与收入结构)、"
        "competitive_position(行业地位与护城河)、vs_market_leader(与板块龙头/大盘的对比，标注 inferred 或来源等级)、"
        "score(0-100 业务画像分，可 null)、data_gaps(取不到的数据项数组，禁止编造)、narrative(一段结论)。"
    )
    if get_config().data_source_profile == "choice":
        import json
        from src.agent.tools.data_tools import _handle_get_stock_info
        if research is None:
            if run := current_research.get():
                run.check()
            info = _handle_get_stock_info(stock_code, deadline=deadline)
            research = (info.get("fundamental_context") or {}).get("research") or {}
        research = research or {}
        if run := current_research.get():
            run.retain_vendor("business", research)
        if get_config().enable_ifind_mcp and not research.get("product_segments"):
            from src.agent.tools.data_tools import _get_fetcher_manager
            if run := current_research.get():
                run.check()
            research = {**research, "product_segments": _get_fetcher_manager().get_ifind_research_context(stock_code, "products", deadline=deadline)}
        task += "\n供应商公司资料（各字段来源以载荷标注为准，非独立双源核验；不得将公司简介当定期报告原文）：" + json.dumps(research, ensure_ascii=False)
    else:
        research = {}
    if run := current_research.get():
        run.retain_vendor("business", research)
    out = _run_explore("business", _BUSINESS_TOOLS, task, llm_adapter, progress_callback, max_steps, deadline=deadline)
    company = research.get("company") or {}
    products = research.get("product_segments")
    if out.get("ok"):
        out["data"].pop("product_segments", None)
    if not out.get("ok") and (company.get("MAINBUSINESS") or company.get("MAINPRODUCT") or (products or {}).get("items")):
        out = {**out, "ok": True, "data": {"status": "degraded", "degraded_reason": str(out.get("error") or "研究模型未完成") + "；保留已取得业务资料", "data_gaps": ["竞争地位与同行对比未完成"], "narrative": "以下为已取得供应商结构化资料，未取得定期报告原文及竞争证据，不生成业务评分。"}}
        for field, indicator in (("business_model", "MAINBUSINESS"), ("main_products", "MAINPRODUCT")):
            if company.get(indicator):
                out["data"][field] = {"value": company[indicator], "source": f"Choice {indicator}，截至{research.get('as_of')}"}
    if out.get("ok") and products:
        out["data"]["product_segments"] = products
        if products.get("errors"):
            out["data"]["data_gaps"] = list(dict.fromkeys([*(out["data"].get("data_gaps") or []), *products["errors"]]))
    out["vendor_research"] = research
    return out
