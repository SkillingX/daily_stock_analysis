# -*- coding: utf-8 -*-
"""探索型维度 Agent：S3 情报 / L4 产业链（唯二需要 ReAct 工具循环的维度）。

工具子集精选（factory.py 模式），max_steps=8；输出强制 JSON，契约校验失败重试 1 次。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from src.agent.runner import run_agent_loop
from src.agent.tools.registry import ToolRegistry
from src.deep_research_dims.narrate import load_constitution, load_dim_prompt

logger = logging.getLogger(__name__)

import threading

from src.config import get_config

_LLM_SEMAPHORE: "threading.Semaphore | None" = None


def _llm_semaphore() -> "threading.Semaphore":
    """全局 LLM 并发上限（MiniMax code plan 有限流）：所有研究员探索循环共用。"""
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        limit = int(getattr(get_config(), "deep_research_llm_concurrency", 3) or 3)
        _LLM_SEMAPHORE = threading.Semaphore(max(1, limit))
    return _LLM_SEMAPHORE


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
) -> Dict[str, Any]:
    """跑一轮探索循环，返回 LLM 最终 JSON dict；失败重试 retry 次。"""
    system = (
        load_constitution()
        + "\n\n"
        + load_dim_prompt(dim_id)
        + "\n\n最终回答必须是且仅是一个 JSON 对象（不要 Markdown 代码围栏外的任何文字）。"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_task},
    ]
    registry = _build_registry(_registry_source(), tool_names)

    last_error = ""
    for attempt in range(retry + 1):
        with _llm_semaphore():
            loop = run_agent_loop(
                messages=messages,
                tool_registry=registry,
                llm_adapter=llm_adapter,
                max_steps=max_steps,
                progress_callback=progress_callback,
                max_wall_clock_seconds=480.0,
                stock_scope=None,
            )
        parsed = _extract_json(loop.content)
        if parsed is not None:
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


def run_capital_agent(stock_code, stock_name, llm_adapter, progress_callback=None, max_steps=8):
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
    return _run_explore("capital", _CAPITAL_TOOLS, task, llm_adapter, progress_callback, max_steps)


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


def run_ownership_agent(stock_code, stock_name, llm_adapter, progress_callback=None, max_steps=8):
    """股权高管研究员：实控人/十大股东/高管背景与变动（审计 A2：评分只作参考，不硬塞六维）。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做股权架构与高管背景调查并直接输出 JSON：\n"
        "1) search_knowledge_base 查已有资料，search_comprehensive_intel / search_stock_news "
        "查实控人/股东/高管的公开信息（同花顺数据源为本，搜索为补）；\n"
        "2) 输出 JSON：controller(实际控制人/控股股东，一句话)、top_holders(前十大股东要点数组)、"
        "executives(核心高管：姓名/职务/背景一句，数组)、recent_changes(近一年增减持/任免/股权变动数组)、"
        "data_gaps(取不到的数据项数组，禁止编造)、score(治理结构健康度参考分 0-100，可 null)、"
        "narrative(调查结论一段)。所有事实带来源等级标签。"
    )
    return _run_explore("ownership", _OWNERSHIP_TOOLS, task, llm_adapter, progress_callback, max_steps)


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


_BUSINESS_TOOLS = {"search_comprehensive_intel", "search_stock_news", "get_market_indices"}


def run_business_agent(stock_code, stock_name, llm_adapter, progress_callback=None, max_steps=8):
    """基本面研究员（业务画像）：经营模式/主营产品/竞争地位/与龙头对比。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做业务画像与竞争地位分析并直接输出 JSON：\n"
        "1) search_comprehensive_intel 调研主营业务/经营模式/行业地位，search_stock_news 查业务动态，"
        "get_market_indices 取大盘环境作对比基准；\n"
        "2) 输出 JSON：business_model(一句话经营模式)、main_products(主营产品与收入结构)、"
        "competitive_position(行业地位与护城河)、vs_market_leader(与板块龙头/大盘的对比，标注 inferred 或来源等级)、"
        "score(0-100 业务画像分，可 null)、data_gaps(取不到的数据项数组，禁止编造)、narrative(一段结论)。"
    )
    return _run_explore("business", _BUSINESS_TOOLS, task, llm_adapter, progress_callback, max_steps)
