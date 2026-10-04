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
from typing import Any, Dict, List, Optional

from src.deep_research_dims.narrate import load_constitution

logger = logging.getLogger(__name__)

# 主干节 key → 规则回退标题（LLM 缺席时模板用）
SECTION_FALLBACK_TITLES: Dict[str, str] = {
    "investment": "投资判断",
    "market": "盘面解读",
    "worldview": "走势推演：三种世界观",
    "valuation": "估值锚从哪里来",
}

# 七个维度节 key → 规则回退标题
DIM_SECTION_KEYS: Dict[str, str] = {
    "business": "业务画像与竞争地位",
    "intel": "消息面详析",
    "capital": "资金面分析",
    "technical": "技术面分析",
    "sentiment": "情绪面分析",
    "us_china": "中美竞争分析",
    "ownership": "股权架构与高管",
}

ALL_SECTION_KEYS: List[str] = list(SECTION_FALLBACK_TITLES) + list(DIM_SECTION_KEYS)

_STYLE_RULES = (
    "写作规范（必须遵守）：\n"
    "1. 面向普通投资人，大白话讲清楚；专业名词（PE/PB/ROE/EV/MACD 等）保留原文。\n"
    "2. 只允许使用输入事实中出现的数字，禁止新增或修改任何数字。\n"
    "3. 节标题必须是斩钉截铁的判断句（例如「不是 X，而是 Y」「为什么 X」「X 背后的 Y」），"
    "不超过 40 字，标题内部不要用冒号（用逗号或顿号代替）。\n"
    "4. 每节叙事按四段式写：先给明确判断 → 再讲机制（为什么会这样）→ 引用数据支撑 → "
    "最后给触发条件（什么情况会让这个判断失效，用「跌破/站上/增速低于/毛利率破」这类可验证的触发词，"
    "禁止用「若」「如果」开头的假设句式）。\n"
    "5. 禁用模糊词（零容忍）：可能、或许、大概、也许、一定程度上、或将、有望、预期向好、"
    "存在不确定性、需谨慎、建议关注、值得关注、拭目以待。这些词一个都不允许出现。\n"
    "6. 每个判断必须用确定性动词收口：是/不是、会/不会、已经/尚未、强/弱、值得/不值得、"
    "买/卖/持有/观察、低估/高估、兑现/未兑现、拐点已到/拐点未到。\n"
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
    "12. 结论段必须给出斩钉截铁的操作判断：现在买不买、等什么价格或事件才买、仓位上限多少、"
    "跌破哪里就放弃；禁止给出「中性偏谨慎」「谨慎乐观」这类没有操作含义的话。\n"
    "13. 只输出 JSON，不要输出任何 JSON 以外的内容。"
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
    """风险矩阵清洗：risk/impact/trigger 三键；trigger 必须可验证（含数字或价位词）。"""
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
    """三条赚钱路径：name（路径名）/win_rate（胜率估计）/playbook（触发+目标）。

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


def _clean_decision_matrix(value: Any) -> List[Dict[str, str]]:
    """操作决策矩阵：persona（持仓状态）/action（指令）/price_range/rationale。

    对标"按持仓状态分行"的决策表；不超过 5 行。
    """
    if not isinstance(value, list):
        return []
    out: List[Dict[str, str]] = []
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        persona = _clean_header(item.get("persona"), limit=16)
        action = _clean_narrative(item.get("action"))[:40]
        price_range = _clean_narrative(item.get("price_range"))[:24]
        rationale = _clean_narrative(item.get("rationale"))[:120]
        if persona and action:
            out.append(
                {"persona": persona, "action": action, "price_range": price_range, "rationale": rationale}
            )
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
    llm_adapter: Any, system: str, facts: Dict[str, Any], timeout: float = 150.0
) -> Dict[str, Any]:
    resp = llm_adapter.call_text(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": _dump(facts)},
        ],
        temperature=0.4,
        timeout=timeout,
    )
    return _extract_json((resp.content or "").strip())


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

    constitution = load_constitution()
    expert_persona = _load_expert_persona()
    base_system = (
        constitution
        + ("\n\n" + expert_persona if expert_persona else "")
        + "\n\n你是卖方深度研报的主笔基金经理，正在为这只股票写报告终稿。"
        + _STYLE_RULES
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

    # 波 B：主干四节叙事 + 首屏论点 + 风险矩阵 + 赚钱路径/决策矩阵/情景触发/维度定性
    try:
        parsed = _call_wave(
            llm_adapter,
            base_system
            + "\n\n输出 JSON 契约：{\"section_narratives\": {"
            + ", ".join(f'\"{k}\": \"…(150-300字四段式论述)\"' for k in SECTION_FALLBACK_TITLES)
            + "}, \"thesis_points\": ["
            "{\"header\": \"…(≤20字斩钉截铁判断句)\", \"body\": \"…(3-5句，第一句必须含数字)\"}"
            " × 3 条], \"risk_matrix\": ["
            "{\"risk\": \"…(≤30字)\", \"impact\": \"…(≤40字影响路径)\", \"trigger\": \"…(≤40字可验证阈值，如 毛利率破28%/跌破31.3)\"}"
            " × ≤5 条], \"money_paths\": ["
            "{\"name\": \"…(≤16字路径名，如 财报验证驱动)\", \"win_rate\": \"…(胜率估计，如 约50%)\", \"playbook\": \"…(触发条件+目标位，≤80字)\"}"
            " × ≤3 条，按确定性降序], \"decision_matrix\": ["
            "{\"persona\": \"…(持仓状态，如 空仓者/持仓高位)\", \"action\": \"…(指令)\", \"price_range\": \"…\", \"rationale\": \"…(≤60字)\"}"
            " × ≤5 行], \"scenario_triggers\": {"
            "\"optimistic\": \"…(可验证触发条件)\", \"neutral\": \"…\", \"pessimistic\": \"…\"}}",
            _wave_b_facts(stock_name, stock_code, dims),
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
        out["decision_matrix"] = _clean_decision_matrix(parsed.get("decision_matrix"))
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
        )
        narratives = parsed.get("section_narratives")
        if isinstance(narratives, dict):
            for k in DIM_SECTION_KEYS:
                n = _clean_narrative(narratives.get(k))
                if len(n) >= 30:
                    out["section_narratives"][k] = n
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DualTrack] 经理终稿波C失败: %s", exc)

    return out


def section_title(manager: Optional[Dict[str, Any]], key: str) -> str:
    """模板取标题入口：LLM 标题 → 规则回退。"""
    fallback = SECTION_FALLBACK_TITLES.get(key) or DIM_SECTION_KEYS.get(key) or key
    if not manager:
        return fallback
    title = (manager.get("section_titles") or {}).get(key)
    return title or fallback
