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
    "score 字段仍按下方 schema 照常输出。报告末尾免责声明由聚合层统一附，"
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
_OWNERSHIP_TOOLS = {
    "search_comprehensive_intel",
    "search_stock_news",
    "search_knowledge_base",
}
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
    if ctx.quote and ctx.quote.get("price") is not None:
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
        if quote.get("price") is not None:
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
        return _handle_get_capital_flow(stock_code)

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


def _retain_institution_block(run: Any, block: Dict[str, Any]) -> None:
    """机构标量沿既有 get_fundamental_context 抽取留存（issue #56 A2）。

    值经既有抽取器的资格投影进 run.facts：来源/单位（percentage_point）/
    财期/口径（caliber，如 top10_shareholders 统计对象）/质量与拒绝原因
    等既有 RetainedFact 字段保留，非整条原始 field_meta 归档；股东户数不
    等于持仓，已知限制原样随投影保留。
    """
    data = block.get("data") or {}
    meta = block.get("field_meta") or {}
    payload = {key: data.get(key) for key in ("institution_holding_change", "top10_holder_change")
               if data.get(key) is not None or isinstance(meta.get(key), dict)}
    if run is not None and payload:
        run.retain_facts("get_fundamental_context", {**payload, "field_meta": meta})


def _institution_materials(stock_code: str) -> Dict[str, Any]:
    """机构/股东固定输入：优先消费本报告已取得的 institution 块（issue #56）。

    阶段 0 快照（同报告、同证券）自本票起保留 institution 块，命中时零新增
    出站；仅当该块整体未保留（旧日缓存/装配异常）才走既有有界入口
    ``manager.get_fundamental_context``（choice 下经 #51 single_fetch 与阶段 0
    同飞）。块内失败/空值如实保留，不整装配 stock_info，不因缺项重复出站。
    """
    run = current_research.get()
    if run is not None:
        run.check()
    ctx = getattr(run, "context", None)
    fundamental = getattr(ctx, "fundamental", None) or {}
    block = fundamental.get("institution")
    if isinstance(block, dict):
        block = _supplement_holder_change(stock_code, fundamental, block)
        _retain_institution_block(run, block)
        return block
    from src.agent.tools.data_tools import _get_fetcher_manager

    if run is None:
        run = current_research.get()
    if run is not None:
        run.check()
    from src.config import get_config

    budget = max(0.0, get_config().fundamental_stage_timeout_seconds)
    try:
        context = _get_fetcher_manager().get_fundamental_context(stock_code, budget_seconds=budget)
    except Exception as exc:  # noqa: BLE001 - 机构资料失败如实降级
        return {"status": "failed", "data": {}, "field_meta": {}, "errors": [str(exc)]}
    fetched = context.get("institution")
    if not isinstance(fetched, dict):
        return {"status": "failed", "data": {}, "field_meta": {}, "errors": ["机构资料块缺失"]}
    fetched = _supplement_holder_change(stock_code, context, fetched)
    _retain_institution_block(run, fetched)
    return fetched


def _supplement_holder_change(stock_code: str, fundamental: Dict[str, Any], block: Dict[str, Any]) -> Dict[str, Any]:
    """Reuse the current snapshot and fetch only the previous quarter within one budget."""
    config = get_config()
    if config.data_source_profile != "choice" or not getattr(config, "enable_ifind_mcp", False) or any(
        record.get("rule_eligible") for record in (block.get("field_meta") or {}).values()
    ):
        return block
    details = (fundamental.get("research") or {}).get("shareholder_details")
    if not details:
        return block
    from datetime import timedelta
    import json
    from data_provider.ifind_fundamental_adapter import IfindShareholders, shareholder_concentration_change
    from data_provider.cross_source_validator import AnchorQuality, AnchorReading, SourceReadError, adopted_field_record
    from src.agent.tools.data_tools import _get_fetcher_manager

    try:
        current = IfindShareholders.model_validate_json(json.dumps(details))
        periods = {item.report_date for item in current.items}
        if current.errors:
            raise ValueError("current_top10_unavailable: " + "; ".join(current.errors))
        if current.coverage != "complete" or len(periods) != 1:
            raise ValueError("current_top10_incomplete")
        period = next(iter(periods))
        end = date.fromisoformat(period)
        if end > date.today():
            raise ValueError("holder_period_in_future")
        if (end.month, end.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
            raise ValueError("holder_period_not_quarter_end")
        previous_period = (date(end.year, ((end.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)).isoformat()
        run = current_research.get()
        if run is not None:
            run.check()
        deadline = monotonic() + max(0.0, config.fundamental_fetch_timeout_seconds)
        if run is not None:
            deadline = min(deadline, run.deadline)
        raw = _get_fetcher_manager().get_ifind_research_context(stock_code, "holders", period=previous_period, deadline=deadline)
        previous = IfindShareholders.model_validate_json(json.dumps(raw))
        if previous.errors:
            raise ValueError("previous_top10_unavailable: " + "; ".join(previous.errors))
        if any(item.report_date != previous_period for item in previous.items):
            raise ValueError("previous_holder_period_mismatch")
        change, current_ratio, previous_ratio = shareholder_concentration_change(current, previous)
        reading = AnchorReading("ifind", float(change), period=period, unit="percentage_point", source_family="ths", caliber="top10_shareholder_concentration")
        record = {**adopted_field_record("top10_holder_change", reading, AnchorQuality(status="single_source"), selection_reason="computed_top10_concentration_change"),
                  "previous_period": previous_period, "current_ratio": float(current_ratio), "previous_ratio": float(previous_ratio)}
        result = {**block, "status": "partial", "data": {**(block.get("data") or {}), "top10_holder_change": float(change)},
                  "field_meta": {**(block.get("field_meta") or {}), "top10_holder_change": record},
                  "source_chain": [*(block.get("source_chain") or []), {"provider": "ifind", "result": "partial"}],
                  "note": "仅比较两期各自前十大股东集中度；不是全体机构持仓变化或净买卖。披露日/原文未取得，不用于历史时点可得性判定。"}
        if run is not None:
            run.check()
            fundamental["institution"] = result
        return result
    except (ValueError, SourceReadError) as exc:
        return {**block, "errors": list(dict.fromkeys([*(block.get("errors") or []), f"iFinD两期股东集中度未取得：{exc}"]))}


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


# 三个固定金额窗口：任务内展示与失败兜底 flow_summary 同一来源（issue #56）。
_FLOW_WINDOWS: tuple[tuple[str, str], ...] = (
    ("main_net_inflow", "当日"),
    ("inflow_5d", "5交易日"),
    ("inflow_10d", "10交易日"),
)


def _flow_display_text(flow: Optional[Dict[str, Any]]) -> str:
    """字段级资格合格金额的既有展示文本，供任务直接引用、避免模型自行换算。"""
    if flow is None or flow.get("main_net_inflow") is None or not _flow_unit_known(flow):
        return ""
    record = (flow.get("field_meta") or {}).get("main_inflow") or {}
    unit = str(record.get("unit") or "")
    amounts = "；".join(_flow_amount_text(flow, key, label, unit) for key, label in _FLOW_WINDOWS)
    return f"主力净流入金额展示（代码按合格单位换算，直接引用，不自行换算）：{amounts}。\n"


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


def _capital_gap_reason(flow_ready: bool, institution_eligible: bool, flow: Dict[str, Any], institution: Dict[str, Any]) -> str:
    parts: list[str] = []
    if not flow_ready:
        parts.append(f"资金流不可用：{_flow_gap_text(flow)}")
    if not institution_eligible:
        status = institution.get("status")
        parts.append("机构/股东资料无合格字段" + (f"（{status}）" if status else ""))
    return "；".join(parts)


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


def _capital_task(stock_code: str, stock_name: str, flow: Optional[Dict[str, Any]], institution: Dict[str, Any], market_cap: Optional[Dict[str, Any]] = None) -> str:
    """资金研究员任务文本：代码准备的固定资料注入，单轮无工具分析（issue #56）。"""
    import json

    if market_cap is not None:
        market_cap_text = (
            "\n3) 市值锚点（同报告估值资料，整条复用既有字段级资格记录，含来源/单位/币种/时点/"
            "财期/质量与拒绝原因；单源数据未独立核验，不当作跨源核验结论；资金量级判断可对照此"
            "分母，禁止把已取得的市值说成缺失；gaps 中的原资格记录按其拒绝原因如实保留，"
            "record 为 null 即无记录，不虚构市值数字）："
            + json.dumps(market_cap, ensure_ascii=False)
        )
    else:
        market_cap_text = "\n3) 市值锚点：本报告未取得合格市值资料（按真实缺口说明，不虚构市值数字）。"
    return (
        f"请对 A 股 {stock_name}（{stock_code}）做资金面深度分析并直接输出 JSON。\n"
        "解读方向：结合市值锚点说明资金流的相对量级含义；机构/股东部分说明既有口径"
        "（field_meta 的 caliber/period/period_basis）实际能支持的结论范围。\n"
        "本次所需的固定资料已由代码全部准备完毕；你没有可用工具，也不要请求任何工具，仅基于以下事实完成分析：\n"
        "1) 资金流资料（已取得，权威；金额缺失、窗口不完整或单位未确认按缺口处理）："
        + json.dumps(flow, ensure_ascii=False)
        + ("\n" + flow_display if (flow_display := _flow_display_text(flow)) else "")
        + "\n2) 机构/股东资料（institution 块，含来源与资格标注；股东户数不等于持仓量）："
        + json.dumps(institution, ensure_ascii=False)
        + market_cap_text
        + "\n4) 输出 JSON：flow_summary、flow_score(0-100 或 null)、institution_summary"
        "（机构/大户持仓变动解读）、institution_score(0-100 或 null)、score(两项加权总分或 null，任一侧缺失不给合成总分)、"
        "narrative(50-100 字结论：只引用资料中实际存在的硬数字——净流入金额/持仓变动比例/市值锚点等；"
        "可引用的数字不足时写足实际存在的并明确其余缺口，禁止为凑数量编造)。\n"
        "金额缺失、窗口不完整或单位未确认时，flow_score、资金总分填 null，不推断资金金额、方向或交易行动；"
        "narrative 只说明已验证内容与真实缺口，不要求凑齐数字。"
        "机构与股东资料按各自 field_meta/source_chain 引用；缺统计对象、日期或单位时只作线索，不给机构分数或方向结论；"
        "top10_shareholder_concentration 仅表示两期各自十大股东集中度之差（百分点），"
        "不代表全体机构增减仓、同一批股东净买卖或退出前十者清仓；机构评分在此仅作股东集中度参考。"
        "某一侧缺失时保留另一侧的解释，禁止把局部缺项说成整套资料无效。"
    )


def run_capital_agent(stock_code: str, stock_name: str, llm_adapter: Any, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None, max_steps: int = 8) -> Dict[str, Any]:
    """资金研究员：代码准备资金流与机构资料，单轮无工具结构化分析（issue #56）。"""
    flow = _capital_flow_material(stock_code)
    institution = _institution_materials(stock_code)
    # 市值材料是同报告共享上下文的纯读取/留存（零新增出站），必须在门控早退
    # 前执行：两侧无效不启动模型时，已知市值仍进 run.facts 供失败/取消留存。
    market_cap = _market_cap_materials()
    institution_eligible = any(record.get("rule_eligible") for record in (institution.get("field_meta") or {}).values())
    flow_ready = flow is not None and flow.get("main_net_inflow") is not None and _flow_unit_known(flow)
    if not flow_ready and not institution_eligible:
        flow_reason = _flow_gap_text(flow or {})
        return {"ok": False, "error": f"无可解释的有效资金资料：资金流——{flow_reason}；机构/股东资料无合格字段，未启动模型"}
    task = _capital_task(stock_code, stock_name, flow, institution, market_cap)
    out = _run_explore("capital", set(), task, llm_adapter, progress_callback, max_steps=1)
    if not out.get("ok"):
        return out
    data = out["data"]
    if not institution_eligible:
        data["institution_score"] = None
        data["institution_summary"] = "公开机构/股东资料仅作线索；统计对象、日期或单位未核实，不能据此判定持仓方向。"
    if not flow_ready:
        data["flow_score"] = None
        data["flow_summary"] = _flow_gap_text(flow or {})
    if not flow_ready or not institution_eligible:
        # 单腿缺失时不给合成总分；另一侧已取得的解释保留在 narrative 与对应 summary。
        data["score"] = None
        data["status"] = "degraded"
        data["degraded_reason"] = _capital_gap_reason(flow_ready, institution_eligible, flow or {}, institution)
    if flow_ready:
        assert flow is not None
        record = (flow.get("field_meta") or {}).get("main_inflow") or {}
        unit = str(record.get("unit") or "")
        amounts = "；".join(_flow_amount_text(flow, key, label, unit) for key, label in _FLOW_WINDOWS)
        source_label = "Choice 主力净流入" if flow.get("source") == "choice" else "主力净流入"
        as_of_text = f"，截至{flow.get('expected_date')}已收盘交易日" if flow.get("expected_date") else ""
        data["flow_summary"] = f"{source_label}{as_of_text}：{amounts}。单源数据，未独立核验。"
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


def run_ownership_agent(stock_code: str, stock_name: str, llm_adapter: Any, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None, max_steps: int = 8, *, deadline: Optional[float] = None, research: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """股权高管研究员：实控人/十大股东/高管背景与变动（审计 A2：评分只作参考，不硬塞六维）。"""
    task = (
        f"请对 A 股 {stock_name}（{stock_code}）做股权架构与高管背景调查并直接输出 JSON：\n"
        "1) search_knowledge_base 查已有资料，search_comprehensive_intel / search_stock_news "
        "查实控人/股东/高管的公开信息（结构化资料优先，搜索为补）；\n"
        "2) 输出 JSON：controller(实际控制人/控股股东，一句话)、top_holders(前十大股东要点数组)、"
        "executives(核心高管：姓名/职务/背景一句，数组)、recent_changes(近一年增减持/任免/股权变动数组)、"
        'data_gaps(未取得资料的字符串数组，如 ["近一年减持公告未取得"]，不得输出对象，禁止编造)、'
        "core_data_gaps(核心缺口字符串数组：控制关系缺少可核实依据、前十大股东资料不足、"
        "核心高管姓名/现任职务未核实必须列入；仅由股东排名推测实控人不能视为核实；"
        "明确披露无实际控制人属于有效结论。三项核心证据均足够时显式输出 []。"
        "完整履历、补充公告/变动、知识库或非股权接口缺项只列 data_gaps，不能单独作为核心缺口；"
        "核心缺口也须列入 data_gaps；不要因存在补充缺口主动把 status 写为 degraded)、"
        "score(治理结构健康度参考分 0-100，可 null)、narrative(调查结论一段)。"
        "top_holders、executives、recent_changes 三个数组每条必须是对象（name 必填，附 source 来源标签，"
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
    out["vendor_research"] = research
    return out


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
    out["vendor_research"] = research
    return out
