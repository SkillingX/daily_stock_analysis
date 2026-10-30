# -*- coding: utf-8 -*-
"""探索型维度 Agent：S3 情报 / L4 产业链（唯二需要 ReAct 工具循环的维度）。

工具子集精选（factory.py 模式），max_steps=8；输出强制 JSON，契约校验失败重试 1 次。
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import date
from functools import partial
from time import monotonic
from typing import Any, Callable, Dict, List, Optional

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
    "get_sector_rankings",
    "get_stock_info",
}

# 两试点（技术/资金）在共享宪法之上的分区职责边界（issue #56 阶段一）：
# 宪法的 PM 七问、评级+目标价+时间维度与免责声明属于规则/聚合层职责，
# 不作为单轮固定资料解读的输出义务；来源等级、缺口披露与禁止编造等
# 既有纪律原样有效，仅收敛与本轮 schema 冲突的报告级义务与取数前时点确认职责。
_PILOT_DIMS = frozenset({"technical", "capital"})
_PILOT_SCOPE = (
    "【本轮职责边界（分区研究员，优先于上文通用要求中与本边界冲突的部分）】\n"
    "你是投研报告众多分区中的一位固定资料解读员：分析所需资料已由代码全部注入，"
    "你没有工具、不重新研究公司；你的 JSON 是既有规则与聚合层的分区输入，不是完整研究报告。\n"
    "职责分工：投资评级（买入/增持/中性/减持等）、目标价、时间维度、交易行动词表"
    "（加仓/持有/减仓等）与 PM 七问投资决策由报告的既有规则与聚合层负责，不在本轮输出义务内；"
    "本维度 schema 定义的评分字段仍照常输出。报告末尾免责声明由聚合层统一附，"
    "你在 JSON 之外不得再输出任何文字。\n"
    "仍然有效的纪律：来源等级标签、数据日期/财期/单位、资格拒绝记录、单源未独立核验的标注、"
    "真实缺口披露与禁止编造数字，全部按上文通用要求执行。\n"
    "数字纪律：资料中的原统计值与代码给定展示是引用依据——已有统计展示（如金额窗口）直接引用，"
    "不重复求和或重算；其他数字只做保留原值含义与币种的等值单位展示（如 2050000000000 CNY 写作 2.05 万亿元），"
    "不新增资料未支持的推导。\n"
    "未知纪律：资料未给出的比较基期、统计范围或子集一律按「未给出」如实说明，不得推定为事实，"
    "也不得把由其推导的金额或结论当成事实。\n"
    "时点纪律：上文中「先确认此刻最新一期财报再取数」的要求在本轮不适用——资料已由代码注入、"
    "你没有取数动作，改为以资料已给定的时点与财期作解读。资料未给出更新核验结果时，"
    "不得断言某记录是全局最新或「当前最新可得披露」，只能说「资料中最晚已给的时点/财期」。"
    "讨论时效或陈旧性时写明所依据的时钟——财期截止日、观测/披露时间与会话日是不同的时点，"
    "不得把财期截止日当作观测或披露时间；不同时钟得出不同结论时如实并陈，"
    "依据不足以判定就说明该限制。\n"
    "本边界不降低分析深度：围绕已注入资料的核心证据、理由与真实缺口充分解读，不略过已给证据，"
    "也不加大全报告成文义务。"
)


# 情报研究员在共享宪法之上的职责收敛（issue #68）：与 _PILOT_SCOPE 同模式，
# 在同一 system 消息内置于宪法之后——宪法第 5 条 PM 七问与第 6 条评级/目标价/
# 时间维度的报告级义务由既有规则与聚合层负责，不在情报研究员输出义务内；
# 事件预案槽位为研究核验/跟踪动作。来源等级、缺口披露与禁止编造等既有纪律
# 原样有效，仅收敛与本轮 schema 冲突的报告级投资决策义务。
_INTEL_SCOPE = (
    "【本轮职责边界（分区情报研究员，优先于上文通用要求中与本边界冲突的部分）】\n"
    "投资评级（买入/增持/中性/减持等）、目标价、时间维度、交易行动词表与宪法第 5 条"
    " PM 七问中的投资决策问项，由报告的既有规则与聚合层负责，不在本轮输出义务内；\n"
    "引用券商/机构评级或目标价时保留原文并带来源等级，不得自行给出评级、目标价或买卖结论；\n"
    "event_calendar.plans 只写研究核验/跟踪动作（披露后对照公告原文核验阈值、跟踪数据发布等），"
    "不写买入/卖出/建仓/加仓/减仓/止损/止盈/仓位等交易指令，不使用 T1 买卖编号；"
    "涉及操作条件时统一写「操作条件以本报告规则作战计划为准」；\n"
    "价格依据：系统任务中注入的本报告已采用行情是唯一现价口径，搜索摘录报价属文献；\n"
    "仍然有效的纪律：来源等级标签、数据日期/时点、真实缺口披露与禁止编造数字，全部按上文通用要求执行。"
)


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
        + ("\n\n" + _PILOT_SCOPE if dim_id in _PILOT_DIMS else "")
        + ("\n\n" + _INTEL_SCOPE if dim_id == "intel" else "")
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
        # The transport succeeded but the structured answer is unusable:
        # attribute the parse failure to the attempt that just returned.
        from src.llm.attempt import refine_attempt_parse_error

        refine_attempt_parse_error()
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


def _quote_eligibility_text(meta: Dict[str, Any]) -> str:
    """行情字段资格的真实投影（issue #68）：拒绝/质量/限制原样传递，缺字段未知。

    ``rule_eligible`` 三态区分（True 合格 / False 不合格 / 缺失未知）；资格文案
    只投影资格本身，不从 True 推断「单源/未独立核验」——``quality.status``
    （如 verified/conflict）与 ``input_reasons`` 单独如实携带，避免与已核验或
    冲突元数据自相矛盾。
    """
    eligible = meta.get("rule_eligible")
    if eligible is True:
        text = "资格 合格"
    elif eligible is False:
        text = "资格 不合格"
    else:
        text = "资格 未知"
    reasons = [str(reason) for reason in (meta.get("input_reasons") or [])]
    if reasons:
        text += f"；资格限制 {', '.join(reasons)}"
    quality_status = str(((meta.get("quality") or {}).get("status")) or "")
    if quality_status and quality_status != "unverified":
        text += f"；质量状态 {quality_status}"
    return text


def _intel_market_context() -> str:
    """本报告已采用行情依据（issue #68）：与报告其他维度同一标量与资格。

    行情来自阶段 0 共享上下文（零新增出站）；无有效行情时如实披露缺失，
    禁止用搜索摘录补造现价。文献摘录报价只能作为带来源/时点的引用。
    无效标量（0/负/NaN/Inf/bool）与缺价同界：不构成已采用现价，不得以
    权威口径注入（spec 反例）。
    """
    from src.deep_research_dims.dim_cache import valid_quote_price

    run = current_research.get()
    quote = getattr(getattr(run, "context", None), "quote", None) or {}
    if not valid_quote_price(quote.get("price")):
        return (
            "【本报告行情依据（权威）】本次研究未取得有效实时行情：没有已采用的现价、涨跌幅或价格位置。"
            "禁止把搜索摘录或新闻中的任何报价当作本次现价、当前涨跌，也不得据此推算相对目标空间或盘面位置；"
            "引用文献报价必须写明属摘录并带来源，无行情时点就写「日期未知」。"
        )
    meta = (quote.get("field_meta") or {}).get("current_price") or {}
    parts = [f"现价 {quote.get('price')}"]
    parts.append(f"行情时点 {meta.get('observed_at') or '未记录'}")
    parts.append(f"来源 {meta.get('source') or '未记录'}")
    parts.append(_quote_eligibility_text(meta))
    return (
        "【本报告已采用行情（权威依据，你的盘面与价格分析必须与此一致，禁止矛盾或声称缺失）】"
        + "；".join(parts)
        + "。\n价格依据纪律：以上是本报告唯一已采用的现价；搜索工具返回内容中的任何报价/涨跌幅均为文献摘录，"
        "不得当作本次现价或当前盘面涨跌；引用文献报价必须写明属摘录并带来源与日期（无日期如实写「日期未知」）；"
        "禁止用摘录报价替换或覆盖上述已采用行情。"
    )


def run_intel_agent(
    stock_code: str,
    stock_name: str,
    llm_adapter: Any,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_steps: int = 8,
) -> Dict[str, Any]:
    """S3 情报探索：新闻/公告/风险/催化剂 + 贝叶斯证据候选。

    issue #68：输入携带本报告已采用行情及来源/时点/资格（system 层职责边界
    见 _INTEL_SCOPE）；event_calendar.plans 只写研究核验/跟踪动作，操作条件
    引用规则作战计划，不再要求独立交易指令或 T1 编号。
    """
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）执行情报收集并直接输出 JSON：\n"
        f"0) {_intel_market_context()}\n"
        "1) 先用 search_stock_news 查个股新闻公告，search_comprehensive_intel 查宏观/行业情报，"
        "get_market_indices 取大盘环境；\n"
        "2) 输出 JSON 字段：sentiment_summary(字符串)、earnings_outlook(字符串)、"
        "risk_alerts(字符串数组)、positive_catalysts(字符串数组)、latest_news(字符串)、"
        "unverified_count(整数)、"
        "evidence_items(数组，元素：{evidence,strength,lr,date}；strength 取 "
        "strong_positive/weak_positive/neutral/weak_negative/strong_negative 之一，"
        "lr 为该证据似然比(正数)，date 为 YYYY-MM-DD；只收录近 90 天证据，最多 6 条；"
        "引用券商/机构评级或目标价时保留原文并带来源等级)；\n"
        "3) root_cause(对象：{event, mechanism, magnitude, persistence})——"
        "当前盘面结构(涨/跌/震荡)的根因：发生了什么事件、通过什么机制影响股价、"
        "影响量级(正面利好/负面利空/有限)、预计持续性；价格水平与涨跌必须与上述已采用行情一致，"
        "基于你搜到的情报作答，搜不到就留空；\n"
        "4) event_calendar(数组，最多 6 项：{event, date, "
        "outcomes(字符串数组，2-4 个可能结果), plans(字符串数组，研究核验/跟踪动作)}；"
        "outcomes 与 plans 必须是字符串数组，每条动作/结果一个数组元素，"
        "不要把多条内容并成一个字符串)——只收录当前时点之后尚未发生的事件"
        "(财报/解禁/政策/行业会议等)，禁止列出已过去年份的事件；每个事件给出可能结果和对应的"
        "研究核验/跟踪动作；不确定日期写\"未知\"；outcomes 只写事实性可能结果，不夹带交易指令。\n"
        "来源等级只使用 primary/news/industry/inferred/unverified，写在字符串内容里；F10 聚合页和二手引用不得标 primary，推断必须写明依据。不要求社区评价，也不要写固定品牌双源验证字样。"
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
    """L4 产业链探索：图谱/定位/瓶颈；概念归属取 iFinD 最新（公开双源校验已退役）。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）执行产业链调研并直接输出 JSON：\n"
        "1) 用 search_comprehensive_intel 调研产业链位置与瓶颈，get_sector_rankings 看板块归属，"
        "get_stock_info 取基本面与最新概念归属（iFinD，概念标签不是客户事实）；\n"
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

_SENTIMENT_TOOLS = {"search_stock_news"}
_US_CHINA_TOOLS = {"search_comprehensive_intel", "get_stock_info"}


def _chanlun_structure_facts(history: list[dict[str, Any]]) -> str:
    """缠论引擎结构（issue #42/#56）：只消费已准备的同一份日线，不自行取数。

    空/不足 60 根或引擎失败返回空串，作为真实缺项披露；取数统一走
    ``_prepared_history`` 的同报告单飞，避免失败样本重复出站。
    """
    if len(history) < 60:
        return ""
    try:
        import pandas as pd

        from chanlun.chanlun_engine import ChanLunEngine

        df = pd.DataFrame(history)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        out = ChanLunEngine(df).analyze()
    except Exception:  # noqa: BLE001 - 引擎失败不阻断研究员
        return ""
    # 引擎结构结论沿既有标量抽取留存（issue #56 A2）：模型失败/取消后退化
    # 报告仍保留已算好的趋势/位置/背驰，文本结论不算评分依据。
    if run := current_research.get():
        run.retain_facts("chanlun_engine", {"source": "缠论引擎计算", "date": history[-1].get("date") or "",
                                            "current_trend": out.get("current_trend"),
                                            "position": out.get("position"),
                                            "divergence": out.get("divergence")})
    return (
        f"【缠论引擎结构（权威，禁止矛盾）】趋势={out.get('current_trend')}"
        f"，中枢位置={out.get('position')}，背驰={out.get('divergence') or '无'}；"
        f"笔数={len(out.get('strokes') or [])}，中枢数={len(out.get('zhongshus') or [])}。"
    )


def _shared_market_facts(ctx: Any, history: Optional[List[Dict[str, Any]]] = None, history_source: str = "") -> str:
    """共享快照中已取得的技术事实摘要（同次研究、同分析时点，issue #42）。

    现价、最新收盘（含真实数据日期）、MA20、支撑阻力与数据来源均来自
    阶段 0 共享快照；技术研究传入同报告单飞补取的日线时，以同一份结果
    注入（history/history_source 覆盖），指标复用既有计算入口。
    """
    if ctx is None:
        return ""
    if history is None:
        history = getattr(ctx, "history", None) or []
    if not history_source:
        history_source = str(getattr(ctx, "history_source", "") or "")
    parts: list[str] = []
    from src.deep_research_dims.dim_cache import valid_quote_price

    if ctx.quote and valid_quote_price(ctx.quote.get("price")):
        parts.append(f"现价 {ctx.quote['price']}")
    if history:
        closes = [c for r in history if (c := r.get("close")) is not None]
        latest = history[-1]
        if closes and latest.get("date"):
            parts.append(
                f"最新收盘 {latest['close']}（数据截至 {str(latest['date'])[:10]}，共 {len(closes)} 根日线）"
            )
        elif closes:
            parts.append(f"日线 {len(closes)} 根（收盘区间 {min(closes)}–{max(closes)}）")
        if len(closes) >= 20:
            ma20 = round(sum(float(c) for c in closes[-20:]) / 20, 2)
            parts.append(f"MA20 {ma20}")
        try:
            from src.deep_research_dims.data_dim import _support_resistance

            support, resistance = _support_resistance(list(history))
            # 已算支撑/阻力沿既有标量抽取留存（issue #56 A2）：模型失败/取消
            # 后退化报告仍保留；None 项由抽取层跳过，不补造。
            if run := current_research.get():
                run.retain_facts("support_resistance", {"source": f"代码计算，日线来源 {history_source or '未记录'}",
                                                        "date": latest.get("date") or "",
                                                        "support": support, "resistance": resistance})
            if support is not None:
                parts.append(f"支撑 {support}")
            if resistance is not None:
                parts.append(f"阻力 {resistance}")
        except Exception:  # noqa: BLE001 - 指标失败不阻断事实注入
            pass
    source = history_source
    parts.append(f"来源：本次研究共享快照（日线实际来源 {source or '未记录'}）")
    return "；".join(parts)


def _prepared_history(stock_code: str) -> tuple[list[dict[str, Any]], str]:
    """技术研究固定日线：共享快照优先，不足时走既有有界入口一次（issue #56）。

    同报告去重身份：run + 证券 + 技术日线需求 + 刷新标记；失败/空结果如实
    保留，不交给模型规划取数，也不在同一研究员内二次加载。返回 (records, source)。
    """
    if run := current_research.get():
        run.check()
    shared_ctx = getattr(run, "context", None)
    shared_history = list(shared_ctx.history) if getattr(shared_ctx, "history", None) else None
    if shared_history is not None and len(shared_history) >= 60:
        return shared_history, str(getattr(shared_ctx, "history_source", "") or "")

    def _load() -> tuple[list[dict[str, Any]], str]:
        from src.services.history_loader import load_history_df

        df, source = load_history_df(stock_code, days=250)
        if df is None or df.empty:
            return [], str(source or "")
        records: list[dict[str, Any]] = df.to_dict(orient="records")  # type: ignore[assignment]
        for row in records:
            if "date" in row:
                row["date"] = str(row["date"])
        return records, str(source or "")

    if run is None:
        records, source = _load()
    else:
        from src.config import get_config
        from src.core.research_run import supplier_need_key
        from data_provider.mx_data_adapter import financial_refresh

        config = get_config()
        refresh = financial_refresh.get()
        key = supplier_need_key("history", config.data_source_contract, "technical_daily", stock_code, refresh[1] if refresh else "")
        records, source = run.single_fetch(key, _load)
    if not records and shared_history:
        # 补取失败/为空时保留阶段 0 已取得的部分日线：局部有效资料
        # 不因一次有界补取失败被整套丢弃（issue #56 反例契约）。
        return shared_history, str(getattr(shared_ctx, "history_source", "") or "")
    return records, source


def _computed_indicator_facts(history: list[dict[str, Any]], stock_code: str, history_source: str = "") -> tuple[str, bool]:
    """既有趋势分析入口产出 MACD/RSI/均线状态（代码计算，不新增取数）。

    返回 (事实文本, 是否可用)；样本不足或引擎失败返回空串，由任务文本披露缺口。
    """
    if len(history) < 20:
        return "", False
    try:
        import pandas as pd

        from src.stock_analyzer import StockTrendAnalyzer

        result = StockTrendAnalyzer().analyze(pd.DataFrame(history), stock_code)
    except Exception:  # noqa: BLE001 - 指标失败只作缺口披露
        return "", False

    def _fmt(value: Any, digits: int = 2) -> str:
        return f"{value:.{digits}f}" if isinstance(value, (int, float)) else str(value)

    # 已算指标沿既有 analyze_trend 标量抽取留存（issue #56 A2）：模型失败/
    # 取消后退化报告仍保留真实算得的指标；样本不足的 0 默认值不留存。来源
    # 区分计算者与上游日线来源（补取结果可不同于共享快照来源），末日如实。
    if run := current_research.get():
        scalars: Dict[str, Any] = {
            "ma5": result.ma5, "ma10": result.ma10, "ma20": result.ma20,
            "current_price": result.current_price,
        }
        if result.macd_signal != "数据不足":
            scalars["macd_dif"] = result.macd_dif
        if result.rsi_signal != "数据不足":
            scalars["rsi_6"] = result.rsi_6
        run.retain_facts("analyze_trend", {"source": f"代码指标计算，日线来源 {history_source or '未记录'}",
                                           "date": history[-1].get("date") or "", **scalars})

    # 样本不足时分析器保留 0 默认值并以信号文案标注；默认值不是权威事实，
    # 只披露实际算得的指标，其余按名列为真实缺口（issue #56 根反例）。
    parts: list[str] = [
        f"趋势={result.trend_status.value}",
        f"均线排列={result.ma_alignment}",
        f"MA5={_fmt(result.ma5)}，MA10={_fmt(result.ma10)}，MA20={_fmt(result.ma20)}",
    ]
    if result.macd_signal == "数据不足":
        parts.append("MACD 未取得（样本不足）")
    else:
        parts.append(
            f"MACD DIF={_fmt(result.macd_dif, 4)}/DEA={_fmt(result.macd_dea, 4)}/{result.macd_status.value}（{result.macd_signal}）"
        )
    if result.rsi_signal == "数据不足":
        parts.append("RSI 未取得（样本不足）")
    else:
        parts.append(f"RSI6={_fmt(result.rsi_6)}/RSI12={_fmt(result.rsi_12)}/{result.rsi_status.value}")
    parts.append(f"量能={result.volume_status.value}")
    text = "【已计算指标（代码产出，权威）】" + "；".join(parts)
    return text, True


def _technical_valid_parts(
    shared_ctx: Any, history: list[dict[str, Any]], indicator_facts: str, engine_facts: str
) -> dict[str, bool]:
    """可解释技术资料的组成标记；仅来源文字不算有效资料（issue #56）。"""
    quote_price = (getattr(shared_ctx, "quote", None) or {}).get("price")
    return {
        "quote": quote_price is not None,
        "daily": bool(history),
        "indicators": bool(indicator_facts),
        "structure": bool(engine_facts),
    }


def _technical_gaps(valid: dict[str, bool]) -> str:
    labels = {"quote": "实时行情", "daily": "日线历史", "indicators": "MACD/RSI/均线指标", "structure": "缠论结构"}
    missing = [label for name, label in labels.items() if not valid.get(name)]
    return "、".join(missing) if missing else "无"


def _technical_task(
    stock_code: str,
    stock_name: str,
    market_facts: str,
    engine_facts: str,
    indicator_facts: str = "",
    gaps: str = "无",
) -> str:
    """技术研究员任务文本：代码准备的固定事实注入，单轮无工具分析（issue #56）。"""
    return (
        f"请对 A 股 {stock_name}（{stock_code}）做技术面深度分析并直接输出 JSON。\n"
        "本次所需的固定资料已由代码全部准备完毕；你没有可用工具，也不要请求任何工具，"
        "仅基于以下事实完成分析：\n"
        f"0) 共享快照事实（本次研究已取得，权威，禁止声称这些行情缺失）：{market_facts or '共享快照无行情（缺口如实说明）'}\n"
        f"0.1) 已计算指标（代码产出，权威，你的解读必须与之一致）：{indicator_facts or '未取得（缺口如实说明）'}\n"
        f"0.2) 缠论结构事实（引擎已算好，你的解读必须与引擎一致，禁止矛盾）：{engine_facts or '引擎结构不可用（缺口如实说明）'}\n"
        f"0.3) 已确认缺项：{gaps}。缺项只作为真实限制说明，"
        "禁止把局部缺项说成整套行情缺失，禁止编造未提供的数字或指标；\n"
        "1) 输出 JSON：chanlun_summary(字符串：基于引擎结构的解读——为什么是这个趋势/位置/背驰)、"
        "support(数值或 null)、resistance(数值或 null)：只引用上面实际存在的价位依据，无依据的点位保持 null；"
        "indicator_summary(字符串：MACD/RSI/均线状态，未取得的项如实说明)、"
        "wave_note(字符串：波浪理论视角的一句判断，不适用则写不适用)、"
        "score(0-100 综合技术分或 null：依据不足时保持 null，有分时须与引擎结构方向一致)、"
        "basis(打分依据一句话，只引用实际存在的数字)、"
        "narrative(50-100 字结论：引用实际存在的硬数字——价位/百分比/指标值；"
        "可引用的数字不足时写足实际存在的并明确其余缺口，禁止为凑数量编造)。"
    )


def run_technical_agent(
    stock_code: str,
    stock_name: str,
    llm_adapter: Any,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_steps: int = 8,
) -> Dict[str, Any]:
    """技术研究员：代码准备行情/日线/指标/结构事实，单轮无工具结构化分析（issue #56）。"""
    history, history_source = _prepared_history(stock_code)
    engine_facts = _chanlun_structure_facts(history)
    indicator_facts, _indicators_ok = _computed_indicator_facts(history, stock_code, history_source)
    run = current_research.get()
    shared_ctx = getattr(run, "context", None)
    # 备料标量沿既有 retained_tool_facts 抽取留存（issue #56 A2）：模型失败/
    # 取消/预算中断后退化报告仍保留阶段 0 已取得的行情与日线，不重新取数。
    if run is not None:
        quote = getattr(shared_ctx, "quote", None) or {}
        from src.deep_research_dims.dim_cache import valid_quote_price

        if valid_quote_price(quote.get("price")):
            run.retain_facts("get_realtime_quote", quote)
        if history:
            run.retain_facts("get_daily_history", {"data": [history[-1]], "source": history_source})
    market_facts = _shared_market_facts(shared_ctx, history=history, history_source=history_source)
    valid = _technical_valid_parts(shared_ctx, history, indicator_facts, engine_facts)
    if not any(valid.values()):
        return {"ok": False, "error": "无可解释的有效技术资料（行情、日线、指标、结构均缺失），未启动模型"}
    task = _technical_task(
        stock_code, stock_name, market_facts, engine_facts,
        indicator_facts=indicator_facts, gaps=_technical_gaps(valid),
    )
    return _run_explore("technical", set(), task, llm_adapter, progress_callback, max_steps=1)


def _stage_zero_capital_flow(stock_code: str, run: Any) -> Optional[Dict[str, Any]]:
    """同报告阶段 0 已取得的合格资金块，无合格块时返回 None（issue #58）。

    股票身份、交易时点、窗口覆盖、来源、单位与字段资格记录随块原样到达；
    部分窗口按既有投影如实保留有效性，只有缺块或金额/量纲不合格才回退
    既有有界补查。主块零重取、不跨报告、不放宽资格；既有 opt-in 交叉验证
    对复用块同样生效（验证关闭时为空操作、零新增出站，开启时核验二源按
    既有语义出站——与有界取数路径完全同语义）。
    """
    from src.agent.tools.data_tools import _apply_capital_cross_validation, _capital_flow_tool_result

    ctx = getattr(run, "context", None)
    if ctx is None or getattr(ctx, "stock_code", None) != stock_code:
        return None
    block = (getattr(ctx, "fundamental", None) or {}).get("capital_flow")
    if not isinstance(block, dict) or not block.get("data"):
        return None
    # issue #64/B2：这里的块是本报告阶段0刚取得的 fresh 证据（如周末读取时
    # expected=最近收盘日）——已由取数日历保证合格；缓存合法性在缓存读取边界
    # （快照/manager/日缓存）用共享谓词强制，此处不再叠加守卫误拒 fresh 块。
    flow = _capital_flow_tool_result(stock_code, block)
    if flow.get("main_net_inflow") is None or not _flow_unit_known(flow):
        return None
    from src.config import get_config

    deadline = monotonic() + get_config().fundamental_stage_timeout_seconds
    return _apply_capital_cross_validation(stock_code, flow, block, deadline)


def _capital_flow_material(stock_code: str) -> Optional[Dict[str, Any]]:
    """资金流固定输入：同一资本需求键下阶段 0 合格块复用或既有有界入口
    （issue #58/#56，#51 single_fetch 模式）。

    阶段 0 投影与可选独立源验证在既有 single_fetch 键内完成，与有界路径
    同 supplier/contract/stock/refresh 身份：主块零重取、失败同报告共享，
    重复/并发消费者共享同一次出站并各取深拷贝；无 run 时走既有有界入口。
    """
    from src.agent.tools.data_tools import _handle_get_capital_flow

    run = current_research.get()
    if run is not None:
        run.check()

    def _fetch() -> Dict[str, Any]:
        if run is not None:
            reused = _stage_zero_capital_flow(stock_code, run)
            if reused is not None:
                return reused
        # issue #64：行业资金榜退役——深研资金路径零 sector 出站。
        return _handle_get_capital_flow(stock_code, include_sector_flows=False)

    if run is None:
        return _fetch()
    from src.config import get_config
    from src.core.research_run import supplier_need_key
    from data_provider.mx_data_adapter import financial_refresh

    config = get_config()
    refresh = financial_refresh.get()
    key = supplier_need_key(config.data_source_profile, config.data_source_contract, "capital_flow", stock_code, refresh[1] if refresh else "")
    result = run.single_fetch(key, _fetch)
    run.retain("get_capital_flow", result)
    return result


def _flow_unit_known(flow: Dict[str, Any]) -> bool:
    """金额量纲只认既有资格层的字段级记录（issue #56 根反例契约）。

    完整消费契约：``field_meta.main_inflow`` 带 ``normalization_error``
    （unit_conflict/unit_unknown/metadata_invalid 等资格层明确拒绝）一票
    否决，任何源级标注（currency/source_unit）都不能推翻或升级；无拒绝时
    仅当该记录 ``rule_eligible`` 且带规范 ``unit`` 才算量纲已知。仅有
    ``currency``/``source_unit`` 字段不构成量纲依据，拒绝记录原样保留。
    """
    record = (flow.get("field_meta") or {}).get("main_inflow") or {}
    if record.get("normalization_error"):
        return False
    return bool(record.get("rule_eligible") and record.get("unit"))


def _flow_amount_text(flow: Dict[str, Any], key: str, label: str, unit: str) -> str:
    value = flow.get(key)
    if value is None:
        return f"{label}窗口不完整"
    if unit in {"currency_base", "CNY"}:
        return f"{label} {value / 10000:,.2f} 万元"
    return f"{label} {value}（单位 {unit or '未确认'}）"


# 三个固定金额窗口：任务内展示与 flow_summary 同一来源（issue #56）。
# issue #64：单日窗口不再称「当日」——金额一律随实际截至日展示。
_FLOW_WINDOWS: tuple[tuple[str, str], ...] = (
    ("main_net_inflow", "单日"),
    ("inflow_5d", "近5交易日"),
    ("inflow_10d", "近10交易日"),
)


def _flow_display_text(flow: Optional[Dict[str, Any]]) -> str:
    """字段级资格合格金额的既有展示文本，供任务直接引用、避免模型自行换算。"""
    if flow is None or flow.get("main_net_inflow") is None or not _flow_unit_known(flow):
        return ""
    record = (flow.get("field_meta") or {}).get("main_inflow") or {}
    unit = str(record.get("unit") or "")
    as_of = str(flow.get("as_of_date") or flow.get("expected_date") or "")
    pending = ((flow.get("coverage") or {}).get("pending_day") or {}).get("expected_date")
    amounts = "；".join(_flow_amount_text(flow, key, label, unit) for key, label in _FLOW_WINDOWS)
    if as_of:
        pending_text = f"；{pending}资金窗口尚未完整，上述窗口均截至{as_of}" if pending else ""
        prefix = f"资金数据截至{as_of}（最近完整交易日）{pending_text}"
    else:
        prefix = "资金窗口截至日未取得"
    return f"{prefix}。主力净流入金额展示（代码按合格单位换算，直接引用，不自行换算）：{amounts}。\n"


def _flow_gap_text(flow: Dict[str, Any]) -> str:
    errors = "; ".join(flow.get("errors") or [])
    if errors:
        return errors
    note = str(flow.get("note") or "")
    if note:
        return note
    if flow.get("main_net_inflow") is not None:
        return "主力净流入金额单位未确认，不推断资金金额与方向"
    return "资金流数据不可用"


def _market_cap_materials() -> Dict[str, Any]:
    """市值锚点固定输入：只读同报告阶段 0 已装配的 ``ctx.fundamental``（issue #56）。

    总市值/流通市值分开标识，整条复用既有字段级资格记录（含来源/单位/币种/
    时点/财期/质量与拒绝原因），零新增出站。锚点资格沿用既有字段级口径（与
    ``_flow_unit_known`` 同一语义）：带字段级拒绝（``normalization_error``）
    一票否决，仅 ``rule_eligible`` 且带规范 ``unit`` 与值的记录可作量级锚点，
    源级标注不补造资格；不合格与缺失记录原样保留真实原因。总市值入口与
    ``_safe_fundamental`` 标量装配同键名序列（total_mv/market_cap/
    total_market_cap），总/流通不混用。两个固定入口必各自产出锚点或缺口
    （无记录按 ``record: null`` 如实标缺），不会没有可报告项。
    """
    run = current_research.get()
    if run is not None:
        run.check()
    ctx = getattr(run, "context", None)
    fundamental = getattr(ctx, "fundamental", None) or {}
    records = fundamental.get("field_meta") or {}
    anchors: Dict[str, Any] = {}
    gaps: list[Dict[str, Any]] = []
    entries: list[tuple[str, str, tuple[str, ...]]] = [
        ("total", "总市值", ("total_mv", "market_cap", "total_market_cap")),
        ("circ", "流通市值", ("circ_mv",)),
    ]
    for key, label, names in entries:
        existing = [(name, records[name]) for name in names if isinstance(records.get(name), dict)]
        qualified = [
            (name, record) for name, record in existing
            if not record.get("normalization_error")
            and record.get("rule_eligible") and record.get("unit") and record.get("value") is not None
        ]
        if qualified:
            name, record = qualified[0]
            anchors[key] = {"label": label, "field": name, "record": record}
            continue
        # 既有资格记录（含拒绝原因）原样保留，不折成泛称缺失；无记录才如实标缺。
        for name, record in existing:
            gaps.append({"label": label, "field": name, "record": record})
        if not existing:
            gaps.append({"label": label, "field": list(names), "record": None})
    material: Dict[str, Any] = {"anchors": anchors}
    if gaps:
        material["gaps"] = gaps
    # 市值标量沿既有 get_stock_info 抽取留存（issue #56 A2）：合格锚点与拒绝
    # 记录都带既有资格投影进 run.facts（含口径），模型失败/取消后退化报告仍
    # 保留；无记录的缺口没有标量可留，不补造。
    if run is not None:
        payload: Dict[str, Any] = {}
        meta: Dict[str, Any] = {}
        for entry in (*anchors.values(), *gaps):
            record = entry.get("record")
            if not isinstance(record, dict) or record.get("value") is None:
                continue
            fixed = "total_mv" if entry["label"] == "总市值" else "circ_mv"
            payload[fixed] = record.get("value")
            meta[fixed] = record
        if payload:
            run.retain_facts("get_stock_info", {**payload, "field_meta": meta})
    return material


def _capital_task(stock_code: str, stock_name: str, flow: Optional[Dict[str, Any]], market_cap: Optional[Dict[str, Any]] = None) -> str:
    """资金研究员任务文本：代码准备的资金流与市值锚点注入，单轮无工具分析。

    issue #64：机构/股东侧退役——任务不再注入机构资料，也不再要求机构评分
    或合成总分输出；资金日期以资料中的实际截至日为准，禁止说成报告当天。
    """
    import json

    if market_cap is not None:
        market_cap_text = (
            "\n3) 市值锚点（同报告估值资料，整条复用既有字段级资格记录，含来源/单位/币种/时点/"
            "财期/质量与拒绝原因；单源数据未独立核验，不当作跨源核验结论；资金量级判断可对照此"
            "分母，禁止把已取得的市值说成缺失；市值时点与资金截至日是两个不同时钟——市值观测日"
            "与资金 as_of_date 不同时，仅作量级背景对照，不得构成同日净流入/市值比值；"
            "gaps 中的原资格记录按其拒绝原因如实保留，"
            "record 为 null 即无记录，不虚构市值数字）："
            + json.dumps(market_cap, ensure_ascii=False)
        )
    else:
        market_cap_text = "\n3) 市值锚点：本报告未取得合格市值资料（按真实缺口说明，不虚构市值数字）。"
    return (
        f"请对 A 股 {stock_name}（{stock_code}）做资金面深度分析并直接输出 JSON。\n"
        "解读方向：结合市值锚点说明资金流的相对量级含义。\n"
        "本次所需的固定资料已由代码全部准备完毕；你没有可用工具，也不要请求任何工具，仅基于以下事实完成分析：\n"
        "1) 资金流资料（已取得，权威；金额缺失、窗口不完整或单位未确认按缺口处理）："
        + json.dumps(flow, ensure_ascii=False)
        + ("\n" + flow_display if (flow_display := _flow_display_text(flow)) else "")
        + market_cap_text
        + "\n4) 输出 JSON：flow_summary、flow_score(0-100 或 null)、"
        "narrative(50-100 字结论：只引用资料中实际存在的硬数字——净流入金额/市值锚点等；"
        "可引用的数字不足时写足实际存在的并明确其余缺口，禁止为凑数量编造)。\n"
        "金额缺失、窗口不完整或单位未确认时，flow_score 填 null，不推断资金金额、方向或交易行动；"
        "narrative 只说明已验证内容与真实缺口，不要求凑齐数字。"
        "资金日期以资料中的 as_of_date/expected_date/coverage 为准：金额窗口截至最近已收盘交易日，"
        "禁止把资金窗口写成报告当天「今日/当日」。"
    )


def _flow_windows_complete(flow: Dict[str, Any]) -> bool:
    """资金完成条件（issue #64）：三个窗口共同截至同一实际日期且全部合格。

    仅凭单日有效金额不算合格——10/5 日窗口有洞时整组输入不完整，不得启动
    模型，也不得把局部有效包装成整体结论。
    """
    coverage = flow.get("coverage") or {}
    if not all((coverage.get(str(n)) or {}).get("complete") is True for n in (1, 5, 10)):
        return False
    return bool(flow.get("as_of_date") or flow.get("expected_date"))


def run_capital_agent(stock_code: str, stock_name: str, llm_adapter: Any, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None, max_steps: int = 8) -> Dict[str, Any]:
    """资金研究员：代码准备资金流资料，单轮无工具结构化分析（issue #56/#64）。

    issue #64：机构/股东侧退役——完成条件只看合格资金三窗口，不为退役字段
    查询机构持仓、两期股东集中度或 iFinD 股东明细；机构/合成总分不得由模型
    输出或旧字段复活。三窗口共同合格才启动模型；资金总分 = 合格 flow_score。
    """
    # issue #64：打包现行规则为强制项——缺失/为空时对齐既有 business 守卫显式
    # 失败：不做供应商备料、不启动模型，不静默退回「宪法+试点边界」空规则。
    if not load_dim_prompt("capital").strip():
        return {"ok": False, "error": "资金研究员现行提示词缺失，无法执行研究"}
    flow = _capital_flow_material(stock_code)
    # 市值材料是同报告共享上下文的纯读取/留存（零新增出站），必须在门控早退
    # 前执行：资金无效不启动模型时，已知市值仍进 run.facts 供失败/取消留存。
    market_cap = _market_cap_materials()
    flow_ready = (
        flow is not None
        and flow.get("main_net_inflow") is not None
        and _flow_unit_known(flow)
        and _flow_windows_complete(flow)
    )
    if not flow_ready:
        return {"ok": False, "error": f"无可解释的有效资金资料：资金流——{_flow_gap_text(flow or {})}，未启动模型"}
    task = _capital_task(stock_code, stock_name, flow, market_cap)
    out = _run_explore("capital", set(), task, llm_adapter, progress_callback, max_steps=1)
    if not out.get("ok"):
        return out
    data = out["data"]
    # 退役字段不得由模型输出复活（issue #64）：机构评分/摘要与旧合成总分一律移除。
    for retired in ("institution_score", "institution_summary", "score"):
        data.pop(retired, None)
    # 模型分析有效性（issue #64/R5/R11）：可用 flow_score + 模型分析正文缺一即真实
    # 降级。flow_score 先沿既有研究员输入适配边界（coerce_value）归一（如嵌套
    # {"score": 80}），不并行另造 raw 检查；无效/缺失/布尔与无分析仍降级，合法
    # 零分保留；有效时资金总分 = flow_score（旧合成口径作废）。评分范围/非有限
    # 由 CapitalDim Pydantic 契约约束。
    from src.deep_research.researchers.base import coerce_value

    flow_score = coerce_value("flow_score", data.get("flow_score"))
    # 契约层 CapitalDim 的 lax float 本就接受数字字符串；适配边界同口径归一，
    # 不比契约更窄——数字字符串是离线独立复现的既有解析器兼容形态（真实报告
    # raw 响应未捕获，不据此归因）。非有限值（NaN/Inf 字符串）不归一，仍按
    # 无效评分真实降级。
    if isinstance(flow_score, str):
        try:
            converted = float(flow_score.strip())
        except ValueError:
            converted = None
        if converted is not None and math.isfinite(converted):
            flow_score = converted
    data["flow_score"] = flow_score
    model_analysis = str(data.get("narrative") or "").strip() or str(data.get("flow_summary") or "").strip()
    if not isinstance(flow_score, (int, float)) or isinstance(flow_score, bool) or not model_analysis:
        data["status"] = "degraded"
        data["degraded_reason"] = "资金研究员模型分析未形成可用评分或分析内容；已取得的资金事实保留在固定说明。"
    else:
        data["score"] = float(flow_score)
    assert flow is not None
    record = (flow.get("field_meta") or {}).get("main_inflow") or {}
    unit = str(record.get("unit") or "")
    amounts = "；".join(_flow_amount_text(flow, key, label, unit) for key, label in _FLOW_WINDOWS)
    source_label = "Choice 主力净流入" if flow.get("source") == "choice" else "主力净流入"
    as_of = str(flow.get("as_of_date") or flow.get("expected_date") or "")
    as_of_text = f"，截至{as_of}已收盘交易日" if as_of else ""
    pending = ((flow.get("coverage") or {}).get("pending_day") or {}).get("expected_date")
    pending_text = f"；{pending}资金窗口尚未完整" if pending else ""
    data["flow_summary"] = f"{source_label}{as_of_text}：{amounts}{pending_text}。单源数据，未独立核验。"
    data["as_of_date"] = as_of
    data["expected_date"] = str(flow.get("expected_date") or "")
    return out


def run_sentiment_agent(stock_code, stock_name, llm_adapter, progress_callback=None, max_steps=8):
    """情绪研究员：机构评价（评级/目标价/分歧点，强制来源等级）。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做情绪面深度分析并直接输出 JSON：\n"
        "1) search_stock_news 查机构研报/评级观点；\n"
        "2) 输出 JSON：institute_view(机构评价分层：评级/目标价/分歧点)、institute_score(0-100)、"
        "score(总分)、narrative(50-100 字结论：必须含至少 1 个硬数字——机构数/目标价家数，"
        "取不到数字就如实写「缺 XX 数据」，禁止只给分无论据)。"
    )
    return _run_explore("sentiment", _SENTIMENT_TOOLS, task, llm_adapter, progress_callback, max_steps)


def run_us_china_agent(stock_code, stock_name, llm_adapter, progress_callback=None, max_steps=8):
    """中美竞争研究员：从供应链 Agent 拆出（方案决策 1）。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做中美产业链竞争分析并直接输出 JSON：\n"
        "1) search_comprehensive_intel 调研出口管制/制裁/平行链影响，"
        "get_stock_info 取最新概念归属（只作标签，不作为供应链事实）；\n"
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
    # issue #64（规格 §1 保留项）：阶段0已取得的 Choice 公司原始事实（控制人、带
    # 报告期的十大股东名单）随既有公司资料字段留存展示——不补查、不推断控制链、
    # 不评分，仅按原来源与财期呈现。
    company = research.get("company") or {}
    holders = research.get("holders") or {}
    fact_parts = []
    if company.get("AHOLDER"):
        fact_parts.append(
            f"实际控制人（Choice 原始字段，截至 {research.get('as_of') or '未标注'}）：{company['AHOLDER']}"
        )
    holder_names = holders.get("HOLDNAMER")
    if isinstance(holder_names, str) and holder_names.strip():
        names = "；".join(name.strip() for name in holder_names.split(";") if name.strip())
        fact_parts.append(
            f"十大股东名单（Choice 原始字段，报告期 {holders.get('period') or '未标注'}）：{names}"
        )
    if fact_parts and isinstance(out.get("data"), dict):
        out["data"]["narrative"] = (
            str(out["data"].get("narrative") or "").rstrip()
            + "\n\n公司资料（Choice 原始字段留存，未核实控制链）："
            + "；".join(fact_parts)
            + "。"
        )
    out["vendor_research"] = research
    return out
