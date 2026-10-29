# -*- coding: utf-8 -*-
"""基金经理终稿（对标卖方深度研报体）：观点式标题 / 内容概括 / 分节叙事。

3 波 LLM 调用，数字全部来自 dims 事实注入（与 narrate 同套路，禁止改数字）：
    波 A：标题定调 + 内容概括 + 全部节标题（短输出）
    波 B：主干四节叙事（投资判断 / 盘面解读 / 三种世界观 / 估值锚）
    波 C：七维度节叙事（业务/消息/资金/技术/情绪/中美/股权）

任一波失败仅该波降级：标题回退规则句、叙事回退研究员原文，报告永远能出。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from src.deep_research_dims.narrate import load_constitution

logger = logging.getLogger(__name__)

INCOMPLETE_EVIDENCE_NOTE = "财务估值或资金依据不完整，暂不形成综合目标价或操作建议；有效的独立研究见各维度。"


def manager_evidence_complete(dims: Mapping[str, Any]) -> bool:
    """Global investment prose combines these inputs and cannot outvote their quality."""
    return all(getattr(dims.get(key), "status", None) == "ok"
               for key in ("fundamental", "capital", "scenarios", "conclusion", "signal"))


_DIRECTIVE_WORDS = (
    "买", "卖", "建仓", "加仓", "补仓", "首批", "低吸", "接回", "挂单", "吸纳",
    "止损", "跌破", "止盈", "减仓", "兑现", "清仓", "入场", "进场", "离场", "平仓",
    "增持", "减持", "仓位", "退出",
)


def _rule_decision_matrix(plan: Any) -> List[Dict[str, str]]:
    """操作决策表由规则作战计划统一生成（issue #39 终局语义）。

    可执行动作及触发条件只有一个来源——规则 plan；经理矩阵不采信，
    价格与动作不再经自然语言解析转述（平行解析器既漏检又误删，已移除）。
    """
    sniper = getattr(plan, "sniper_points", None)
    points = {
        key: getattr(sniper, key, None)
        for key in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")
    }
    if sniper is None or not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) > 0
        for v in points.values()
    ):
        strategy = getattr(plan, "position_strategy", None)
        entry = str(getattr(strategy, "entry_plan", None) or "").strip()
        rationale = entry[:100] or "未形成可执行方案，保留观察；触发条件见作战计划"
        return [
            {"persona": "所有持仓状态", "action": "观察", "price_range": "—", "rationale": rationale}
        ]

    def _fmt(value: Any) -> str:
        return f"{float(value):g}"

    return [
        {
            "persona": "空仓/未入场",
            "action": "条件买入",
            "price_range": f"{_fmt(points['ideal_buy'])} / {_fmt(points['secondary_buy'])}",
            "rationale": "回落至首批价且量比≥1.5 触发首批，补仓价更低半档；未取得用户持仓成本，非按现价追高",
        },
        {
            "persona": "已持仓（参考）",
            "action": "止损/止盈纪律",
            "price_range": f"{_fmt(points['stop_loss'])} / {_fmt(points['take_profit'])}",
            "rationale": "跌破止损无条件离场，到达止盈分批兑现；止损锚定方案首批成本，已有持仓按自身成本另行评估",
        },
    ]


def _prose_carries_directive(text: str) -> bool:
    """保守拒绝含交易动作的经理内容块，不推断价格距离或自然语言角色。

    操作只采用规则计划；即使是在转述计划或历史交易，也回退既有研究员正文。
    事实数字本身不触发拒绝，原始研究资料与规则表不会被此边界改写。
    """
    return any(word in str(text or "") for word in _DIRECTIVE_WORDS)


def qualified_manager_writeup(manager: Dict[str, Any], dims: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep evidenced sections; unstructured global claims cannot be separated safely."""
    result = dict(manager)
    complete = manager_evidence_complete(dims)
    if not complete:
        for key in ("title_angle", "executive_summary"):
            result[key] = ""
        for key in ("thesis_points", "risk_matrix", "money_paths", "decision_matrix"):
            result[key] = []
        for key in ("scenario_triggers", "dim_verdicts"):
            result[key] = {}
    # 可执行动作及触发条件由规则作战计划统一生成（issue #39 终局语义）：
    # 经理矩阵不采信，决策表直接采用规则行（含价格与动作）。
    if complete:
        result["decision_matrix"] = _rule_decision_matrix(dims.get("plan"))
    # 经理只解释事实；交易动作块整块回退，不维护平行的指令/价格解析器。
    for key in ("thesis_points", "money_paths", "risk_matrix"):
        result[key] = [
            item for item in (result.get(key) or [])
            if not _prose_carries_directive(" ".join(str(v) for v in (item or {}).values()))
        ]
    result["section_narratives"] = {
        section: ("" if _prose_carries_directive(value) else value)
        for section, value in (manager.get("section_narratives") or {}).items()
        if complete and ((section in dims and dims[section].status == "ok")
                         or section in SECTION_FALLBACK_TITLES)
    }
    result["section_titles"] = {
        section: value for section, value in (manager.get("section_titles") or {}).items()
        if complete and ((section in dims and dims[section].status == "ok")
                         or section in SECTION_FALLBACK_TITLES)
        and not _prose_carries_directive(value)
    }
    for section, dim in (("market", "data"), ("valuation", "scenarios")):
        evidence = dims.get(dim)
        if evidence is not None:
            result["section_narratives"][section] = evidence.narrative
            result["section_titles"][section] = SECTION_FALLBACK_TITLES[section]
    for key in ("title_angle", "executive_summary"):
        if _prose_carries_directive(result.get(key, "")):
            result[key] = ""
    for key in ("scenario_triggers", "dim_verdicts"):
        result[key] = {
            name: value for name, value in (result.get(key) or {}).items()
            if not _prose_carries_directive(value)
        }
    return result

# 主干节 key → 规则回退标题（LLM 缺席时模板用）
SECTION_FALLBACK_TITLES: Dict[str, str] = {
    "investment": "投资判断",
    "market": "盘面解读",
    "worldview": "走势推演：三种世界观",
    "valuation": "估值锚从哪里来",
}

# 维度节 key → 规则回退标题（issue #64：ownership 已退役，不再出现在新报告）
DIM_SECTION_KEYS: Dict[str, str] = {
    "business": "业务画像与竞争地位",
    "intel": "消息面详析",
    "capital": "资金面分析",
    "technical": "技术面分析",
    "sentiment": "情绪面分析",
    "us_china": "中美竞争分析",
}

ALL_SECTION_KEYS: List[str] = list(SECTION_FALLBACK_TITLES) + list(DIM_SECTION_KEYS)

_STYLE_RULES = (
    "写作规范（必须遵守）：\n"
    "1. 面向普通投资人，大白话讲清楚；专业名词（PE/PB/ROE/EV/MACD 等）保留原文。\n"
    "2. 只允许使用输入事实中出现的数字，禁止新增或修改任何数字。\n"
    "3. 节标题必须是斩钉截铁的判断句（例如「不是 X，而是 Y」「为什么 X」「X 背后的 Y」），"
    "不超过 40 字，标题内部不要用冒号（用逗号或顿号代替）。\n"
    "4. 每节叙事按四段式写：先给明确判断 → 再讲机制（为什么会这样）→ 引用数据支撑 → "
    "最后给研究判断的验证条件（例如营收增速低于或毛利率低于某个已知财务阈值，"
    "禁止用「若」「如果」开头的假设句式）。\n"
    "5. 禁用模糊词（零容忍）：可能、或许、大概、也许、一定程度上、或将、有望、预期向好、"
    "存在不确定性、需谨慎、建议关注、值得关注、拭目以待。这些词一个都不允许出现。\n"
    "6. 每个判断必须用确定性动词收口：是/不是、会/不会、已经/尚未、强/弱、值得/不值得、"
    "观察、低估/高估、拐点已到/拐点未到。\n"
    "7. 观点必须独有、有差异化认知：不要重复市场共识（「国产替代空间广阔」「AI 带来增长」这类"
    "所有人都会说的话），必须给出你自己基于输入事实推出的、市场尚未充分定价的判断"
    "（例如「市场按晶闸管给估值，但增长引擎已经切换成 MOSFET，这是定价错位」）。\n"
    "8. 禁用英文内部标签：inferred 写「推断」、unverified 写「未经证实」、data gap 写「数据缺口」。\n"
    "9. 数据缺失就正面说「这一节缺少数据」，禁止编造；更禁止把「检索失败」「搜索源不可用」「数据缺失」"
    "本身当成投资结论。\n"
    "10. 某一节关键数据缺失时，结尾必须给投资人两三条「补齐数据后看什么」的具体建议"
    "（例如：年报披露后看分产品收入拆分；机构持仓季报后看基金进出名单），不要只说缺什么。\n"
    "11. 不要泄露内部流水线状态：禁止出现 score_journal、recommendation_journal、GR8/GR9、"
    "先验 0.27、Edge -0.23 等标签或数值；这些是内部状态，不是投资论点。\n"
    "12. 结论段解释研究依据及下一项待验证的事实；操作及仓位条件只引用报告规则计划，"
    "不得自行生成或重复转述交易指令。\n"
    "13. 全部经理字段（含标题、小计、风险、情景和路径）只作事实分析与定性解释，"
    "不写交易动作或风控执行条件（包括买、卖、建仓、止损、止盈、清仓等表述），"
    "无论是否带价格、是否与方案一致，都不重复；统一写「操作条件见规则作战计划」。"
    "可引用现价、估值、量比等事实数字，财务阈值不等于交易条件。\n"
    "14. 只输出 JSON，不要输出任何 JSON 以外的内容。"
)


def _dump(obj: Any, limit: int = 5000) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)[:limit]


_EXPERT_PERSONA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "experts", "equity-research", "agents", "equity-research-expert.md",
)
_expert_persona_cache: Optional[str] = None


def _load_expert_persona() -> str:
    """研股股人格文件（WorkBuddy equity-research 2.1.2 vendor 副本）。

    经理终稿三波全部以研股股身份主笔；文件缺失时返回空串降级为宪法+风格规则，
    不影响报告生成。缓存一次避免三波重复读盘。
    """
    global _expert_persona_cache
    if _expert_persona_cache is not None:
        return _expert_persona_cache
    try:
        with open(_EXPERT_PERSONA_PATH, encoding="utf-8") as fh:
            _expert_persona_cache = fh.read()
    except OSError:
        logger.warning("[DualTrack] 研股股人格文件缺失，经理终稿降级为宪法+风格规则: %s", _EXPERT_PERSONA_PATH)
        _expert_persona_cache = ""
    return _expert_persona_cache


def _extract_json(text: str) -> Dict[str, Any]:
    """从 LLM 输出中提取第一个 JSON 对象；失败返回 {}。"""
    text = re.sub(
        r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL | re.IGNORECASE
    ).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_title(value: Any) -> str:
    title = re.sub(r"[#*`>\[\]]", "", str(value or "")).strip()
    # 报告主标题已带「深度投研：」，标题内冒号会造成双冒号，统一改逗号
    return title.replace("：", "，")[:45]


def _clean_narrative(value: Any) -> str:
    text = re.sub(
        r"<think(?:ing)?>.*?</think(?:ing)?>", "", str(value or ""), flags=re.DOTALL | re.IGNORECASE
    ).strip()
    text = re.sub(r"^#+\s*", "", text)
    return text[:1500]


_HEADER_END_PUNCT = "，。、；：！？,.;:!?—-"


def _clean_header(value: Any, limit: int = 20) -> str:
    """论点/路径标题：句读边界截断（不在词中间硬切），strip 尾部标点。

    反例（硬切）：「偏离MA250达-21%，。」「赔率不。」——本函数治这个。
    """
    text = _clean_title(value)
    if len(text) <= limit:
        return text.rstrip(_HEADER_END_PUNCT)
    # 在 limit 内找最后一个句读，切在句读前的完整分句
    cut = max(text.rfind(p, 0, limit + 1) for p in _HEADER_END_PUNCT)
    if cut >= 8:  # 太短的前缀不如直接按字截
        return text[:cut].rstrip(_HEADER_END_PUNCT)
    return text[:limit].rstrip(_HEADER_END_PUNCT)


def _clean_thesis_points(value: Any) -> List[Dict[str, str]]:
    """首屏 ■ 论点清洗：每条 = 粗体判断句 header（≤20字）+ 3-5句含数字 body。"""
    if not isinstance(value, list):
        return []
    out: List[Dict[str, str]] = []
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        header = _clean_header(item.get("header"))[:20]
        body = _clean_narrative(item.get("body"))[:500]
        if header and len(body) >= 30:
            out.append({"header": header, "body": body})
    return out


def _clean_risk_matrix(value: Any) -> List[Dict[str, str]]:
    """风险矩阵：保留事实风险、影响和研究判断验证条件，不生成交易指令。"""
    if not isinstance(value, list):
        return []
    out: List[Dict[str, str]] = []
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        risk = _clean_title(item.get("risk"))[:30]
        impact = _clean_narrative(item.get("impact"))[:120]
        trigger = _clean_narrative(item.get("trigger"))[:80]
        if risk and impact:
            out.append({"risk": risk, "impact": impact, "trigger": trigger})
    return out


def _clean_money_paths(value: Any) -> List[Dict[str, str]]:
    """三条研究路径：name（路径名）/win_rate（胜率估计）/playbook（验证事实）。

    对标卖方"按确定性排序的路径表"；不超过 3 条。
    """
    if not isinstance(value, list):
        return []
    out: List[Dict[str, str]] = []
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        name = _clean_header(item.get("name"), limit=16)
        win_rate = _clean_narrative(item.get("win_rate"))[:20]
        playbook = _clean_narrative(item.get("playbook"))[:200]
        if name and len(playbook) >= 20:
            out.append({"name": name, "win_rate": win_rate, "playbook": playbook})
    return out



def _clean_scenario_triggers(value: Any) -> Dict[str, str]:
    """三情景触发条件：{optimistic/neutral/pessimistic: 可验证条件句}。

    只收三个键，其余丢弃；值截断 80 字。
    """
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for key in ("optimistic", "neutral", "pessimistic"):
        text = _clean_narrative(value.get(key))[:80]
        if text:
            out[key] = text
    return out


_DIM_VERDICT_KEYS = ("基本面", "消息面", "资金面", "情绪面", "技术面", "宏观")


def _clean_dim_verdicts(value: Any) -> Dict[str, str]:
    """六维各一句小计定性（评分表"小计——定性"行的素材）：{维度名: ≤40字判断句}。"""
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for key in _DIM_VERDICT_KEYS:
        text = _clean_narrative(value.get(key))[:40].rstrip("。.")
        if text:
            out[key] = text
    return out


def _call_wave(
    llm_adapter: Any, system: str, facts: Dict[str, Any], timeout: float = 150.0, *, wave: str = ""
) -> Dict[str, Any]:
    from src.llm.attempt import ENTRY_MANAGER_WRITEUP, attempt_scope

    with attempt_scope(ENTRY_MANAGER_WRITEUP, wave):
        resp = llm_adapter.call_text(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": _dump(facts)},
            ],
            temperature=0.4,
            timeout=timeout,
        )
    if getattr(resp, "provider", "") == "error" or getattr(resp, "finish_reason", None) in {"length", "content_filter"}:
        return {}
    parsed = _extract_json((resp.content or "").strip())
    if not parsed:
        from src.llm.attempt import refine_attempt_parse_error

        refine_attempt_parse_error()
    return parsed


def _wave_a_facts(
    stock_name: str,
    stock_code: str,
    dims: Dict[str, Any],
    guardrail_events: List[Any],
) -> Dict[str, Any]:
    """标题与概括只需要评分/信号/根因级别的浓缩事实。"""
    signal = dims.get("signal")
    conclusion = dims.get("conclusion")
    six_dim = dims.get("six_dim")
    intel = dims.get("intel")
    scenarios = dims.get("scenarios")
    framework = getattr(six_dim, "framework", None)
    root_cause = getattr(intel, "root_cause", None)
    # B5：非估值模式事实带范围说明，经理波次不得请求/编造概率、目标价或收益。
    nonvaluation = (
        getattr(scenarios, "status", None) == "ok"
        and getattr(scenarios, "valuation_basis", None) == "none"
        and getattr(scenarios, "target_price", None) is None
    )
    return {
        "stock": f"{stock_name}（{stock_code}）",
        "total_score": getattr(framework, "dimension_total", None),
        "scoring_version": getattr(six_dim, "scoring_version", None),
        "rating": getattr(signal, "rating", None),
        "action": getattr(getattr(conclusion, "conclusion", None), "action", None),
        "one_sentence": getattr(signal, "one_sentence", None),
        "dimension_summaries": {
            d.dimension: (d.indicators[0].summary if d.indicators else "")
            for d in (framework.dimensions if framework else [])
        },
        "root_cause": root_cause,
        "expected_value": getattr(scenarios, "expected_value", None),
        "target_price": getattr(scenarios, "target_price", None),
        "valuation_basis": getattr(scenarios, "valuation_basis", None),
        "nonvaluation_scope": (
            "本报告无目标价，采用非估值分析：不给目标价/预期收益/胜率估计，"
            "不写估值驱动买卖建议；结论表述为观察与条件跟踪。" if nonvaluation else ""
        ),
        "guardrail_count": len(guardrail_events),
        "guardrail_rules": [getattr(e, "rule_id", "") for e in guardrail_events],
    }


def _wave_b_facts(
    stock_name: str, stock_code: str, dims: Dict[str, Any]
) -> Dict[str, Any]:
    """主干四节：信号/结论/计划/情景/贝叶斯/数据/消息全量事实。"""
    keys = ("signal", "conclusion", "plan", "scenarios", "bayesian", "data", "intel")
    return {
        "stock": f"{stock_name}（{stock_code}）",
        "dims": {k: dims[k].model_dump() for k in keys if k in dims},
    }


def _wave_c_facts(
    stock_name: str, stock_code: str, dims: Dict[str, Any]
) -> Dict[str, Any]:
    """七维度节：对应研究员维度事实。"""
    return {
        "stock": f"{stock_name}（{stock_code}）",
        "dims": {k: dims[k].model_dump() for k in DIM_SECTION_KEYS if k in dims},
    }


def generate_manager_writeup(
    llm_adapter: Any,
    stock_name: str,
    stock_code: str,
    dims: Dict[str, Any],
    guardrail_events: List[Any],
) -> Dict[str, Any]:
    """生成基金经理终稿；任何一波失败只降级该波，整体不抛异常。"""
    out: Dict[str, Any] = {
        "title_angle": "",
        "executive_summary": "",
        "section_titles": {},
        "section_narratives": {},
        "thesis_points": [],
        "risk_matrix": [],
        "money_paths": [],
        "decision_matrix": [],
        "scenario_triggers": {},
        "dim_verdicts": {},
    }
    if llm_adapter is None:
        return out
    if not manager_evidence_complete(dims):
        # Independent researcher facts remain in dims and the template. A manager
        # rewrite can introduce global actions even inside an otherwise valid section.
        return qualified_manager_writeup(out, dims)

    constitution = load_constitution()
    expert_persona = _load_expert_persona()
    # B5：非估值范围指令一致供给全部波次（base_system 条件注入）——禁止任何
    # 胜率/概率数字、目标价或收益空间猜测；观察结论合法保留。
    scenarios_dim = dims.get("scenarios")
    nonvaluation_active = (
        getattr(scenarios_dim, "status", None) == "ok"
        and getattr(scenarios_dim, "valuation_basis", None) == "none"
        and getattr(scenarios_dim, "target_price", None) is None
    )
    nonvaluation_rule = (
        "\n\n【本报告范围（必须遵守）】该报告无目标价，采用非估值分析：全部输出"
        "（含标题、概括、论点、风险、路径与情景触发）不得出现任何胜率/概率估计"
        "（如「约50%」）、目标价、预期收益或估值驱动买卖建议；路径的 win_rate 一律"
        "写「不提供」；结论表述为观察与条件跟踪，不视为看多或看空。"
        if nonvaluation_active
        else ""
    )
    base_system = (
        constitution
        + ("\n\n" + expert_persona if expert_persona else "")
        + "\n\n你是卖方深度研报的主笔基金经理，正在为这只股票写报告终稿。"
        + _STYLE_RULES
        + nonvaluation_rule
    )

    # 波 A：标题定调 + 内容概括 + 全部节标题
    try:
        parsed = _call_wave(
            llm_adapter,
            base_system
            + "\n\n输出 JSON 契约：{\"title_angle\": \"…(≤25字定性短语)\", "
            "\"executive_summary\": \"…(150-250字，按：定性→判断公式→成立条件→跟踪清单 四句式)\", "
            "\"section_titles\": {"
            + ", ".join(f'\"{k}\": \"…\"' for k in ALL_SECTION_KEYS)
            + "}, \"dim_verdicts\": {"
            "\"基本面\": \"…(≤30字小计定性)\", \"消息面\": \"…\", \"资金面\": \"…\", \"情绪面\": \"…\", \"技术面\": \"…\", \"宏观\": \"…\"}}",
            _wave_a_facts(stock_name, stock_code, dims, guardrail_events),
            wave="wave_a_title_summary",
        )
        out["title_angle"] = _clean_title(parsed.get("title_angle"))[:25]
        out["executive_summary"] = _clean_narrative(parsed.get("executive_summary"))[:600]
        titles = parsed.get("section_titles")
        if isinstance(titles, dict):
            for k in ALL_SECTION_KEYS:
                t = _clean_title(titles.get(k))
                if t:
                    out["section_titles"][k] = t
        out["dim_verdicts"] = _clean_dim_verdicts(parsed.get("dim_verdicts"))
    except Exception as exc:  # noqa: BLE001 - 单波失败只降级
        logger.warning("[DualTrack] 经理终稿波A失败: %s", exc)

    # 波 B：主干四节叙事 + 首屏论点 + 风险矩阵 + 赚钱路径/情景触发/维度定性（决策表由规则生成，模型不产出）
    try:
        parsed = _call_wave(
            llm_adapter,
            base_system
            + "\n\n输出 JSON 契约：{\"section_narratives\": {"
            + ", ".join(f'\"{k}\": \"…(150-300字四段式论述)\"' for k in SECTION_FALLBACK_TITLES)
            + "}, \"thesis_points\": ["
            "{\"header\": \"…(≤20字斩钉截铁判断句)\", \"body\": \"…(3-5句，第一句必须含数字)\"}"
            " × 3 条], \"risk_matrix\": ["
            "{\"risk\": \"…(≤30字)\", \"impact\": \"…(≤40字影响路径)\", \"trigger\": \"…(≤40字事实验证条件，如 毛利率低于已披露阈值，不写交易条件)\"}"
            " × ≤5 条], \"money_paths\": ["
            "{\"name\": \"…(≤16字路径名，如 财报验证驱动)\", \"win_rate\": \"…(胜率估计，如 约50%)\", \"playbook\": \"…(研究验证事件与依据，≤80字；操作条件只引用规则作战计划)\"}"
            " × ≤3 条，按确定性降序], \"scenario_triggers\": {"
            "\"optimistic\": \"…(可验证触发条件)\", \"neutral\": \"…\", \"pessimistic\": \"…\"}}",
            _wave_b_facts(stock_name, stock_code, dims),
            wave="wave_b_main_sections",
        )
        narratives = parsed.get("section_narratives")
        if isinstance(narratives, dict):
            for k in SECTION_FALLBACK_TITLES:
                n = _clean_narrative(narratives.get(k))
                if len(n) >= 30:
                    out["section_narratives"][k] = n
        out["thesis_points"] = _clean_thesis_points(parsed.get("thesis_points"))
        out["risk_matrix"] = _clean_risk_matrix(parsed.get("risk_matrix"))
        out["money_paths"] = _clean_money_paths(parsed.get("money_paths"))
        out["scenario_triggers"] = _clean_scenario_triggers(parsed.get("scenario_triggers"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DualTrack] 经理终稿波B失败: %s", exc)

    # 波 C：七维度节叙事
    try:
        parsed = _call_wave(
            llm_adapter,
            base_system
            + "\n\n输出 JSON 契约：{\"section_narratives\": {"
            + ", ".join(f'\"{k}\": \"…(150-300字四段式论述)\"' for k in DIM_SECTION_KEYS)
            + "}}",
            _wave_c_facts(stock_name, stock_code, dims),
            wave="wave_c_dim_sections",
        )
        narratives = parsed.get("section_narratives")
        if isinstance(narratives, dict):
            for k in DIM_SECTION_KEYS:
                n = _clean_narrative(narratives.get(k))
                if len(n) >= 30:
                    out["section_narratives"][k] = n
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DualTrack] 经理终稿波C失败: %s", exc)

    return qualified_manager_writeup(out, dims)


def section_title(manager: Optional[Dict[str, Any]], key: str) -> str:
    """模板取标题入口：LLM 标题 → 规则回退。"""
    fallback = SECTION_FALLBACK_TITLES.get(key) or DIM_SECTION_KEYS.get(key) or key
    if not manager:
        return fallback
    title = (manager.get("section_titles") or {}).get(key)
    return title or fallback
