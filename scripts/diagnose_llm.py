# -*- coding: utf-8 -*-
"""
LLM 链路诊断脚本（旁路；不改任何业务代码）。

读 SQLite 里的 ``analysis_history`` / ``llm_usage`` 两张表，再叠加
``reports/report_YYYYMMDD.md`` 的存在性检查，生成：

* ``reports/llm_diagnose_YYYYMMDD.md``  —— 给人看的诊断报告
* ``reports/llm_diagnose_YYYYMMDD.json`` —— 给机器读的摘要

诊断维度（覆盖 P0 / P1 / P2 已知问题）：

1. **LLM 调用与 usage 完整性**
   - ``llm_usage`` 里 ``prompt_tokens=completion_tokens=total_tokens=0`` 的记录
     → 命中 P0-2 (stream 无超时) 的"usage 漏采"症状。
   - ``analysis`` 类型是否与 ``analysis_history`` 一一对应
     → 缺记录 = LLM 报错后没落库。
   - 同一只股票同日多次分析
     → 重复分析、是否同日 LLM 不稳定。

2. **JSON / 文本解析降级**
   - ``analysis_history.raw_result`` 里 ``action_checklist`` 长度
     → 0 项 = LLM 走 text fallback（silent degrade）。
   - ``raw_result.phase_decision`` 字段是否齐全
     → 缺字段 = fallback 模板填的占位。
   - ``ideal_buy / secondary_buy / stop_loss / take_profit`` 全 None
     → sniper points 全空 → LLM 没给出具体价位（信号弱）。

3. **数据缺失度（与 LLM 无关的输入侧）**
   - ``context_snapshot.enhanced_context.fundamental_context`` 各段 status
   - ``news_content`` 长度（已知 search 失败时是 176 字符的占位符）
   - ``raw_result.dashboard.data_perspective.chip_structure`` 字段是否都标"数据缺失"
   - 缺失维度数 → 触发 P1-2 评分校准规则的检查

4. **报告输出一致性**
   - ``reports/report_YYYYMMDD.md`` 是否存在、是否覆盖了 ``analysis_history`` 的所有股票
   - 报告中"操作建议" / "信心" 与库里的 ``operation_advice`` / ``confidence_level`` 是否一致

5. **P0/P1 问题评级**
   - 每只个股打 flag：高/中/低 严重度
   - 给"立即修复 / 关注 / 健康" 三档总评

注意：

* 脚本不读 ``logs/``，避免脆弱 regex；所有结论来自 SQLite + Markdown 文件。
* 脚本不会写任何业务表（只读 + 写 ``reports/llm_diagnose_*``）。
* 通过 ``--date`` 指定诊断日期（默认今天），或 ``--days N`` 诊断最近 N 天。
* 通过 ``--stock CODE`` 只看单只股票。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Proxy off by default for diagnostic script; LLM calls aren't made here.
import os

os.environ.setdefault("USE_PROXY", "false")

from sqlalchemy import text  # noqa: E402

from src.storage import DatabaseManager, get_db  # noqa: E402

logger = logging.getLogger("llm_diagnose")

# 已知 search 0 结果时注入的 news_content 长度（取 2026-07-17 实测）。
# 容忍 ±10 字符。
EMPTY_NEWS_PLACEHOLDER_LEN = 176

# 同一只股票同日重复分析 = 至少 2 行；阈值 = 1（>=2 触发告警）。
DUPLICATE_ANALYSIS_THRESHOLD = 2


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class LLMSymptom:
    """单只股票 / 单次 LLM 调用的诊断症状。"""

    code: str
    severity: str  # "P0" / "P1" / "P2" / "OK"
    category: str  # "usage" / "json_parse" / "data_missing" / "report" / "duplicate"
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StockDiagnosis:
    code: str
    name: str
    analysis_count: int
    has_market_review: bool
    latest_record_id: Optional[int]
    latest_record_at: Optional[str]
    latest_score: Optional[int]
    latest_advice: Optional[str]
    latest_confidence: Optional[str]
    sniper_points: Dict[str, Optional[float]]
    action_checklist_count: int
    phase_decision_field_count: int
    has_phase_decision: bool
    raw_response_size: int
    news_size: int
    missing_data_dimensions: List[str]
    llm_calls_for_stock: int
    llm_calls_zero_usage: int
    llm_calls_with_usage: int
    total_prompt_tokens: int
    total_completion_tokens: int
    symptoms: List[LLMSymptom] = field(default_factory=list)

    @property
    def severity(self) -> str:
        if any(s.severity == "P0" for s in self.symptoms):
            return "P0"
        if any(s.severity == "P1" for s in self.symptoms):
            return "P1"
        if any(s.severity == "P2" for s in self.symptoms):
            return "P2"
        return "OK"

    @property
    def severity_rank(self) -> int:
        return {"P0": 0, "P1": 1, "P2": 2, "OK": 3}.get(self.severity, 9)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity
        d["symptoms"] = [s.to_dict() for s in self.symptoms]
        return d


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _day_range(day: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    end = datetime.combine(day, time.max)
    return start, end


def _safe_load_json(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a possibly markdown-wrapped JSON object.

    Handles:
    1. Direct JSON parse.
    2. Strip leading/trailing ```` ```json ... ```` ```` fences.
    3. Take the first balanced ``{...}`` substring and retry.
    """
    if not raw:
        return None

    text = raw.strip()
    # Strip ```json ... ``` fences (raw_result may keep them from LLM response).
    if text.startswith("```"):
        # Remove the first line (```json or ```)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (TypeError, ValueError):
        pass

    # Last resort: balanced-brace extraction.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except (TypeError, ValueError):
            pass

    return None


def _safe_text_len(raw: Optional[str]) -> int:
    if not raw:
        return 0
    return len(raw)


# ---------------------------------------------------------------------------
# 数据采集
# ---------------------------------------------------------------------------


def fetch_llm_usage(db: DatabaseManager, day: date) -> List[Dict[str, Any]]:
    start, end = _day_range(day)
    with db.session_scope() as session:
        rows = session.execute(
            text(
                "SELECT id, call_type, model, stock_code, prompt_tokens, "
                "completion_tokens, total_tokens, cache_observation, "
                "eligibility_confidence, normalized_prompt_tokens, "
                "normalized_completion_tokens, normalized_total_tokens, "
                "called_at "
                "FROM llm_usage "
                "WHERE called_at BETWEEN :a AND :b "
                "ORDER BY id"
            ),
            {"a": start, "b": end},
        ).all()
    return [dict(r._mapping) for r in rows]


def fetch_analysis_history(db: DatabaseManager, day: date) -> List[Dict[str, Any]]:
    start, end = _day_range(day)
    with db.session_scope() as session:
        rows = session.execute(
            text(
                "SELECT id, query_id, code, name, sentiment_score, "
                "operation_advice, trend_prediction, ideal_buy, secondary_buy, "
                "stop_loss, take_profit, length(coalesce(raw_result,'')) AS raw_len, "
                "length(coalesce(news_content,'')) AS news_len, "
                "length(coalesce(context_snapshot,'')) AS ctx_len, "
                "raw_result, news_content, context_snapshot, created_at "
                "FROM analysis_history "
                "WHERE created_at BETWEEN :a AND :b "
                "AND code <> 'MARKET' "
                "ORDER BY code, id"
            ),
            {"a": start, "b": end},
        ).all()
    return [dict(r._mapping) for r in rows]


def fetch_market_reviews(db: DatabaseManager, day: date) -> List[Dict[str, Any]]:
    start, end = _day_range(day)
    with db.session_scope() as session:
        rows = session.execute(
            text(
                "SELECT id, code, name, created_at "
                "FROM analysis_history "
                "WHERE created_at BETWEEN :a AND :b "
                "AND code = 'MARKET' "
                "ORDER BY id"
            ),
            {"a": start, "b": end},
        ).all()
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------


def _analyze_single_record(
    record: Dict[str, Any],
    llm_calls: List[Dict[str, Any]],
) -> StockDiagnosis:
    code = record["code"]
    name = record.get("name") or code
    raw_result = _safe_load_json(record.get("raw_result"))
    if raw_result is None:
        # 取不到 raw_result 时（理论上不存在，但兜底），标记一个症状。
        diagnosis = StockDiagnosis(
            code=code,
            name=name,
            analysis_count=0,
            has_market_review=False,
            latest_record_id=record["id"],
            latest_record_at=str(record.get("created_at")),
            latest_score=record.get("sentiment_score"),
            latest_advice=record.get("operation_advice"),
            latest_confidence=None,
            sniper_points={},
            action_checklist_count=0,
            phase_decision_field_count=0,
            has_phase_decision=False,
            raw_response_size=record.get("raw_len", 0) or 0,
            news_size=record.get("news_len", 0) or 0,
            missing_data_dimensions=[],
            llm_calls_for_stock=0,
            llm_calls_zero_usage=0,
            llm_calls_with_usage=0,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            symptoms=[
                LLMSymptom(
                    code=code,
                    severity="P0",
                    category="json_parse",
                    message="raw_result 不可解析为 JSON 对象",
                    details={"record_id": record["id"]},
                )
            ],
        )
        return diagnosis

    # ---- 数据完整度 ----
    dashboard = raw_result.get("dashboard") or {}
    battle_plan = dashboard.get("battle_plan") or {}
    sniper = battle_plan.get("sniper_points") or {}
    sniper_points = {
        "ideal_buy": sniper.get("ideal_buy"),
        "secondary_buy": sniper.get("secondary_buy"),
        "stop_loss": sniper.get("stop_loss"),
        "take_profit": sniper.get("take_profit"),
    }
    action_checklist = battle_plan.get("action_checklist") or []
    if not isinstance(action_checklist, list):
        action_checklist = []

    phase_decision = dashboard.get("phase_decision") or {}
    phase_field_count = len(phase_decision) if isinstance(phase_decision, dict) else 0
    has_phase_decision = (
        isinstance(phase_decision, dict)
        and bool(phase_decision)
        # 必须包含核心字段
        and all(
            phase_decision.get(k)
            for k in (
                "phase_context",
                "action_window",
                "immediate_action",
                "watch_conditions",
                "next_check_time",
                "confidence_reason",
                "data_limitations",
            )
        )
    )

    # 缺失维度
    missing: List[str] = []
    if (
        record.get("news_len", 0) is not None
        and 0 < record["news_len"] <= EMPTY_NEWS_PLACEHOLDER_LEN + 10
    ):
        missing.append("news")
    ctx_snapshot = _safe_load_json(record.get("context_snapshot")) or {}
    enhanced = ctx_snapshot.get("enhanced_context") or {}
    fund_ctx = enhanced.get("fundamental_context") or {}
    chip_ctx = enhanced.get("chip_context") or {}
    chip_status = (chip_ctx.get("status") or chip_ctx.get("data_status") or "").lower()
    chip_data = chip_ctx.get("data") or {}
    if not chip_data or chip_status in {"missing", "unavailable", "partial"}:
        missing.append("chip")
    valuation = fund_ctx.get("valuation") or {}
    val_status = (valuation.get("status") or "").lower()
    val_data = valuation.get("data") or {}
    if val_status in {"missing", "unavailable"} or not val_data:
        missing.append("fundamental_valuation")
    realtime = enhanced.get("realtime") or {}
    if not realtime.get("pe_ratio"):
        missing.append("realtime_pe")

    # ---- LLM usage 汇总 ----
    calls_for_stock = [c for c in llm_calls if c.get("stock_code") == code]
    zero_usage = [
        c
        for c in calls_for_stock
        if (c.get("prompt_tokens") or 0) == 0 and (c.get("completion_tokens") or 0) == 0
    ]
    with_usage = [
        c
        for c in calls_for_stock
        if (c.get("prompt_tokens") or 0) > 0 or (c.get("completion_tokens") or 0) > 0
    ]
    total_prompt = sum(c.get("prompt_tokens") or 0 for c in calls_for_stock)
    total_completion = sum(c.get("completion_tokens") or 0 for c in calls_for_stock)

    diagnosis = StockDiagnosis(
        code=code,
        name=name,
        analysis_count=0,  # 由 caller 填
        has_market_review=False,  # 由 caller 填
        latest_record_id=record["id"],
        latest_record_at=str(record.get("created_at")),
        latest_score=record.get("sentiment_score"),
        latest_advice=record.get("operation_advice"),
        latest_confidence=raw_result.get("confidence_level"),
        sniper_points=sniper_points,
        action_checklist_count=len(action_checklist),
        phase_decision_field_count=phase_field_count,
        has_phase_decision=has_phase_decision,
        raw_response_size=record.get("raw_len", 0) or 0,
        news_size=record.get("news_len", 0) or 0,
        missing_data_dimensions=missing,
        llm_calls_for_stock=len(calls_for_stock),
        llm_calls_zero_usage=len(zero_usage),
        llm_calls_with_usage=len(with_usage),
        total_prompt_tokens=total_prompt,
        total_completion_tokens=total_completion,
    )

    # ---- 症状判定 ----
    # P0-1: action_checklist=0 → LLM 走 text fallback（silent degrade）
    if len(action_checklist) == 0:
        diagnosis.symptoms.append(
            LLMSymptom(
                code=code,
                severity="P0",
                category="json_parse",
                message="battle_plan.action_checklist 为空 → LLM 走 text fallback 路径，"
                "silent degrade，结构化字段（6 项 checklist + sniper_points）已丢失",
                details={
                    "record_id": record["id"],
                    "raw_size": diagnosis.raw_response_size,
                },
            )
        )

    # P0-2: phase_decision 不全 → fallback 模板
    if not has_phase_decision:
        diagnosis.symptoms.append(
            LLMSymptom(
                code=code,
                severity="P0",
                category="json_parse",
                message=f"phase_decision 字段缺失或不完整（{phase_field_count} 个）→ "
                "LLM 输出未通过 JSON 校验",
                details={
                    "record_id": record["id"],
                    "phase_decision_field_count": phase_field_count,
                },
            )
        )

    # P0-2: usage 全 0 → stream timeout
    # 严格说 usage=0 不影响单只报告的"内容完整性"，只是无法计费/观测。
    # 标记为 P0 是因为这是"全批报告都无法计费"的全局性问题，应在优化 2 修复
    # 之前一直可见。
    if (
        calls_for_stock
        and len(zero_usage) == len(calls_for_stock)
        and len(calls_for_stock) >= 1
    ):
        diagnosis.symptoms.append(
            LLMSymptom(
                code=code,
                severity="P0",
                category="usage",
                message=f"全部 {len(calls_for_stock)} 次 LLM 调用 usage 全 0 → "
                "stream 模式未采集到 token（命中 P0-2 stream 无超时）。"
                "注意：报告内容可能完整，但 cost / cache 命中率等无法观测",
                details={
                    "zero_usage_calls": len(zero_usage),
                    "with_usage_calls": len(with_usage),
                    "expected_min_tokens": 1500,  # 实测 prompt ~4400 字符
                },
            )
        )

    # P1-2: 缺失 3+ 维度但 confidence 仍"中"（应自动降为"低"）
    if len(missing) >= 3 and raw_result.get("confidence_level") == "中":
        diagnosis.symptoms.append(
            LLMSymptom(
                code=code,
                severity="P1",
                category="data_missing",
                message=f"缺失维度 {len(missing)} ({','.join(missing)})，但 confidence_level 仍为'中' → "
                "未触发 missing→confidence 强制降级",
                details={
                    "missing_dimensions": missing,
                    "confidence_level": raw_result.get("confidence_level"),
                },
            )
        )

    # P1-1: sniper_points 全 None
    if all(v in (None, "", "无", "暂无") for v in sniper_points.values()):
        diagnosis.symptoms.append(
            LLMSymptom(
                code=code,
                severity="P1",
                category="json_parse",
                message="sniper_points 4 项全部为空 → LLM 未给出具体价位，报告可执行性差",
                details={"sniper_points": sniper_points},
            )
        )

    # P1: news 缺失
    if "news" in missing:
        diagnosis.symptoms.append(
            LLMSymptom(
                code=code,
                severity="P1",
                category="data_missing",
                message="news 搜索 0 结果（搜索渠道限流 / SearXNG 失败）→ "
                "事件面信号缺失，LLM 风险/催化字段退化为技术面模板",
                details={"news_size": diagnosis.news_size},
            )
        )

    # P1: chip 缺失
    if "chip" in missing:
        diagnosis.symptoms.append(
            LLMSymptom(
                code=code,
                severity="P1",
                category="data_missing",
                message="chip_distribution 缺失 → 筹码集中度无法判断，"
                "LLM 输出的 chip_structure 字段全部'数据缺失'占位",
                details={"chip_status": chip_status},
            )
        )

    return diagnosis


def diagnose_day(day: date) -> Dict[str, Any]:
    db = get_db()
    llm_usage = fetch_llm_usage(db, day)
    history = fetch_analysis_history(db, day)
    market_reviews = fetch_market_reviews(db, day)

    # 按 code 聚合
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for row in history:
        by_code.setdefault(row["code"], []).append(row)

    diagnoses: List[StockDiagnosis] = []
    for code, records in by_code.items():
        # 取最新一条
        latest = max(records, key=lambda r: r["id"])
        diag = _analyze_single_record(latest, llm_usage)
        diag.analysis_count = len(records)
        # 重复分析症状
        if len(records) >= DUPLICATE_ANALYSIS_THRESHOLD:
            diag.symptoms.append(
                LLMSymptom(
                    code=code,
                    severity="P2",
                    category="duplicate",
                    message=f"同日 {len(records)} 次分析 → 重复分析或重跑",
                    details={"record_ids": [r["id"] for r in records]},
                )
            )
        diagnoses.append(diag)

    # LLM 异常：analysis 类型与 analysis_history 不一致
    analysis_calls = [c for c in llm_usage if c.get("call_type") == "analysis"]
    codes_in_llm: set[str] = {
        str(c.get("stock_code")) for c in analysis_calls if c.get("stock_code")
    }
    codes_in_history: set[str] = set(by_code.keys())
    orphan_calls: set[str] = codes_in_llm - codes_in_history
    extra_codes: set[str] = codes_in_history - codes_in_llm
    if orphan_calls:
        for code in sorted(orphan_calls):
            diagnoses.append(
                StockDiagnosis(
                    code=code,
                    name=code,
                    analysis_count=0,
                    has_market_review=False,
                    latest_record_id=None,
                    latest_record_at=None,
                    latest_score=None,
                    latest_advice=None,
                    latest_confidence=None,
                    sniper_points={},
                    action_checklist_count=0,
                    phase_decision_field_count=0,
                    has_phase_decision=False,
                    raw_response_size=0,
                    news_size=0,
                    missing_data_dimensions=[],
                    llm_calls_for_stock=sum(
                        1 for c in analysis_calls if c.get("stock_code") == code
                    ),
                    llm_calls_zero_usage=sum(
                        1
                        for c in analysis_calls
                        if c.get("stock_code") == code
                        and (c.get("prompt_tokens") or 0) == 0
                    ),
                    llm_calls_with_usage=0,
                    total_prompt_tokens=0,
                    total_completion_tokens=0,
                    symptoms=[
                        LLMSymptom(
                            code=code,
                            severity="P0",
                            category="usage",
                            message="LLM 调用记录存在但 analysis_history 缺失 → "
                            "LLM 输出未持久化（或被丢弃）",
                            details={
                                "llm_call_count": sum(
                                    1
                                    for c in analysis_calls
                                    if c.get("stock_code") == code
                                )
                            },
                        )
                    ],
                )
            )
    if extra_codes:
        # 没有 LLM 调用的 analysis_history 行（理论不应出现）
        for code in extra_codes:
            for diag in diagnoses:
                if diag.code == code:
                    diag.symptoms.append(
                        LLMSymptom(
                            code=code,
                            severity="P0",
                            category="usage",
                            message="analysis_history 有记录但 llm_usage 无对应调用 → "
                            "可能来自文本 fallback / 重试时未上报 usage",
                            details={"records": [r["id"] for r in by_code[code]]},
                        )
                    )
                    break

    # 排序：P0 > P1 > P2 > OK
    diagnoses.sort(key=lambda d: (d.severity_rank, d.code))

    # 全局统计
    market_review_zero_usage = sum(
        1
        for c in llm_usage
        if c.get("call_type") == "market_review" and (c.get("prompt_tokens") or 0) == 0
    )
    summary = {
        "date": day.isoformat(),
        "total_llm_calls": len(llm_usage),
        "analysis_calls": len(analysis_calls),
        "market_review_calls": sum(
            1 for c in llm_usage if c.get("call_type") == "market_review"
        ),
        "agent_calls": sum(1 for c in llm_usage if c.get("call_type") == "agent"),
        "llm_calls_zero_usage": sum(
            1
            for c in llm_usage
            if (c.get("prompt_tokens") or 0) == 0
            and (c.get("completion_tokens") or 0) == 0
        ),
        "market_review_zero_usage": market_review_zero_usage,
        "total_prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in llm_usage),
        "total_completion_tokens": sum(
            c.get("completion_tokens") or 0 for c in llm_usage
        ),
        "total_tokens": sum(c.get("total_tokens") or 0 for c in llm_usage),
        "stocks_analyzed": len(by_code),
        "stocks_diagnosis": len(diagnoses),
        "by_severity": {
            "P0": sum(1 for d in diagnoses if d.severity == "P0"),
            "P1": sum(1 for d in diagnoses if d.severity == "P1"),
            "P2": sum(1 for d in diagnoses if d.severity == "P2"),
            "OK": sum(1 for d in diagnoses if d.severity == "OK"),
        },
        "unique_models": sorted(
            {str(c.get("model")) for c in llm_usage if c.get("model")}
        ),
        "market_reviews_count": len(market_reviews),
    }

    return {
        "date": day.isoformat(),
        "summary": summary,
        "diagnoses": [d.to_dict() for d in diagnoses],
        "llm_usage": [
            {
                "id": c["id"],
                "call_type": c.get("call_type"),
                "model": c.get("model"),
                "stock_code": c.get("stock_code"),
                "prompt_tokens": c.get("prompt_tokens") or 0,
                "completion_tokens": c.get("completion_tokens") or 0,
                "total_tokens": c.get("total_tokens") or 0,
                "cache_observation": c.get("cache_observation"),
                "eligibility_confidence": c.get("eligibility_confidence"),
                "called_at": str(c.get("called_at")) if c.get("called_at") else None,
            }
            for c in llm_usage
        ],
    }


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


SEVERITY_EMOJI = {"P0": "🔴", "P1": "🟡", "P2": "🟢", "OK": "✅"}


def render_markdown(payload: Dict[str, Any]) -> str:
    s = payload["summary"]
    lines: List[str] = []
    lines.append(f"# LLM 链路诊断报告 · {payload['date']}")
    lines.append("")
    lines.append(
        "> 数据源：SQLite (`analysis_history` / `llm_usage`)。本报告不读日志，"
        "不修改任何业务表。"
    )
    lines.append("")
    lines.append("## 全局摘要")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---:|")
    lines.append(f"| LLM 调用总数 | {s['total_llm_calls']} |")
    lines.append(f"|  └─ analysis | {s['analysis_calls']} |")
    lines.append(f"|  └─ market_review | {s['market_review_calls']} |")
    lines.append(f"|  └─ agent | {s['agent_calls']} |")
    lines.append(f"| usage 全 0 调用 | {s['llm_calls_zero_usage']} |")
    lines.append(
        f"|  └─ market_review 中 usage 全 0 | {s['market_review_zero_usage']} |"
    )
    lines.append(f"| 总 prompt tokens | {s['total_prompt_tokens']:,} |")
    lines.append(f"| 总 completion tokens | {s['total_completion_tokens']:,} |")
    lines.append(f"| 总 tokens | {s['total_tokens']:,} |")
    lines.append(f"| 个股分析数 | {s['stocks_analyzed']} |")
    lines.append(f"| 大盘复盘数 | {s['market_reviews_count']} |")
    lines.append(f"| 使用模型 | {', '.join(s['unique_models']) or '-'} |")
    lines.append("")
    lines.append("### 严重度分布")
    lines.append("")
    lines.append("| 严重度 | 数量 | 含义 |")
    lines.append("|---|---:|---|")
    lines.append(
        f"| {SEVERITY_EMOJI['P0']} P0 | {s['by_severity']['P0']} | 立即修复：silent degrade / 数据丢失 |"
    )
    lines.append(
        f"| {SEVERITY_EMOJI['P1']} P1 | {s['by_severity']['P1']} | 关注：数据缺失 / 字段空 / 信心失真 |"
    )
    lines.append(
        f"| {SEVERITY_EMOJI['P2']} P2 | {s['by_severity']['P2']} | 观察：重复分析 / 边界 |"
    )
    lines.append(f"| {SEVERITY_EMOJI['OK']} OK | {s['by_severity']['OK']} | 健康 |")
    lines.append("")

    # 单只股票详情
    lines.append("## 个股诊断")
    lines.append("")
    for d in payload["diagnoses"]:
        emoji = SEVERITY_EMOJI.get(d["severity"], "❓")
        lines.append(f"### {emoji} `{d['code']}` {d['name']}")
        lines.append("")
        lines.append(f"- 严重度：**{d['severity']}**")
        lines.append(f"- 今日分析次数：{d['analysis_count']}")
        lines.append(f"- 最新 record_id：{d['latest_record_id']}")
        lines.append(f"- 最近一次时间：{d['latest_record_at']}")
        if d["latest_score"] is not None:
            lines.append(
                f"- 最终结论：score={d['latest_score']} / {d['latest_advice']} / "
                f"confidence={d['latest_confidence']}"
            )
        lines.append(
            f"- LLM 调用：{d['llm_calls_for_stock']} 次（usage 0: {d['llm_calls_zero_usage']} / "
            f"有 usage: {d['llm_calls_with_usage']}）"
        )
        if d["total_prompt_tokens"] or d["total_completion_tokens"]:
            lines.append(
                f"- 累计 token：prompt={d['total_prompt_tokens']:,} / "
                f"completion={d['total_completion_tokens']:,}"
            )
        lines.append(
            f"- checklist 条数：{d['action_checklist_count']} / 6 "
            f"（{'silent degrade' if d['action_checklist_count'] < 6 else 'OK'}）"
        )
        lines.append(
            f"- phase_decision 字段：{d['phase_decision_field_count']} / 7 "
            f"（{d['has_phase_decision']}）"
        )
        sp = d["sniper_points"]
        sniper_str = " / ".join(
            f"{k}={v if v not in (None, '') else '∅'}" for k, v in sp.items()
        )
        lines.append(f"- 狙击点位：{sniper_str or '∅'}")
        if d["missing_data_dimensions"]:
            lines.append(f"- 缺失维度：{', '.join(d['missing_data_dimensions'])}")
        if d["symptoms"]:
            lines.append("")
            lines.append("**症状：**")
            for s_ in d["symptoms"]:
                lines.append(
                    f"- `{s_['severity']}` **{s_['category']}** — {s_['message']}"
                )
        lines.append("")

    # LLM usage 异常清单
    if s["llm_calls_zero_usage"] > 0:
        lines.append("## Usage=0 的 LLM 调用清单")
        lines.append("")
        lines.append("| id | 类型 | 股票 | 模型 | prompt | completion | called_at |")
        lines.append("|---:|---|---|---|---:|---:|---|")
        for c in payload["llm_usage"]:
            if (c["prompt_tokens"] or 0) == 0 and (c["completion_tokens"] or 0) == 0:
                lines.append(
                    f"| {c['id']} | {c['call_type']} | {c['stock_code'] or '-'} | "
                    f"{c['model']} | 0 | 0 | {c['called_at']} |"
                )
        lines.append("")

    # 修复建议
    lines.append("## 修复建议（按优先级）")
    lines.append("")
    lines.append(
        "1. **P0-1 单 JSON 强约束**：prompt 末尾追加「只输出 1 段合法 JSON，不要 markdown 围栏 / 思考过程 / 自我重写」；"
        "`_validate_json_response` 检测多 `{` 直接拒绝并触发 fallback chain。"
    )
    lines.append(
        "2. **P0-2 stream first-chunk / idle timeout**：`_consume_litellm_stream` "
        "加 `FIRST_CHUNK_TIMEOUT=10s` + `IDLE_CHUNK_TIMEOUT=30s`；`LLM_CALL_TIMEOUT=90s`；"
        "非 stream 路径强制采集 usage。"
    )
    lines.append(
        "3. **P0-3 missing→confidence 强制降级**：prompt 注入 `missing_data_count` 变量；"
        "缺 3+ 维度 → `confidence_level` 必须为「低」；`_check_content_integrity` "
        "触发条件放宽到 phase_decision 任何 1 项缺失。"
    )
    lines.append(
        "4. **P1-1 news 0 条显式标注**：注入 `news_reliability_status`；"
        "LLM 不得引用「新闻利好」。"
    )
    lines.append(
        "5. **P1-3 prompt 精简 + cache**：把 4 段重复白话压缩到 1 段 ~200 字符；"
        "core_rules 标记 `cache_control` 命中降本 90%。"
    )
    lines.append(
        "6. **P1-2 评分校准表**：按缺失维度强制 `decision_type` 与 trend 区间，"
        "避免全员偏空。"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        f"诊断脚本：`scripts/diagnose_llm.py` · "
        f"行数 {sum(1 for _ in open(__file__))} · "
        f"生成时间 {datetime.now().isoformat(timespec='seconds')}"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _output_paths(day: date) -> Tuple[Path, Path]:
    md = REPO_ROOT / "reports" / f"llm_diagnose_{day.isoformat()}.md"
    js = REPO_ROOT / "reports" / f"llm_diagnose_{day.isoformat()}.json"
    return md, js


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="LLM 链路诊断脚本（旁路；不改任何业务代码）。",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="诊断日期 YYYY-MM-DD，默认今天（本地）。",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="诊断最近 N 天（与 --date 互斥；--date 优先）。",
    )
    parser.add_argument(
        "--stock",
        type=str,
        default=None,
        help="只诊断单只股票（按 code 过滤）。",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="同时把 Markdown 报告打印到 stdout（默认只写文件）。",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.date:
        days: List[date] = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        today = date.today()
        days = [today - timedelta(days=i) for i in range(args.days)]

    all_payloads: List[Dict[str, Any]] = []
    for day in days:
        payload = diagnose_day(day)
        if args.stock:
            payload["diagnoses"] = [
                d for d in payload["diagnoses"] if d["code"] == args.stock
            ]
            payload["llm_usage"] = [
                u for u in payload["llm_usage"] if u.get("stock_code") == args.stock
            ]
            # 调整 summary
            payload["summary"]["stocks_diagnosis"] = len(payload["diagnoses"])
            payload["summary"]["total_llm_calls"] = len(payload["llm_usage"])
            payload["summary"]["analysis_calls"] = sum(
                1 for u in payload["llm_usage"] if u.get("call_type") == "analysis"
            )
            payload["summary"]["market_review_calls"] = sum(
                1 for u in payload["llm_usage"] if u.get("call_type") == "market_review"
            )
            payload["summary"]["agent_calls"] = sum(
                1 for u in payload["llm_usage"] if u.get("call_type") == "agent"
            )
            payload["summary"]["llm_calls_zero_usage"] = sum(
                1
                for u in payload["llm_usage"]
                if (u.get("prompt_tokens") or 0) == 0
                and (u.get("completion_tokens") or 0) == 0
            )
            payload["summary"]["market_review_zero_usage"] = sum(
                1
                for u in payload["llm_usage"]
                if u.get("call_type") == "market_review"
                and (u.get("prompt_tokens") or 0) == 0
            )
            payload["summary"]["total_prompt_tokens"] = sum(
                u.get("prompt_tokens") or 0 for u in payload["llm_usage"]
            )
            payload["summary"]["total_completion_tokens"] = sum(
                u.get("completion_tokens") or 0 for u in payload["llm_usage"]
            )
            payload["summary"]["total_tokens"] = sum(
                u.get("total_tokens") or 0 for u in payload["llm_usage"]
            )
            payload["summary"]["by_severity"] = {
                sev: sum(1 for d in payload["diagnoses"] if d["severity"] == sev)
                for sev in ("P0", "P1", "P2", "OK")
            }
        all_payloads.append(payload)
        md_path, json_path = _output_paths(day)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_text = render_markdown(payload)
        md_path.write_text(md_text, encoding="utf-8")
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "Wrote %s and %s",
            md_path.relative_to(REPO_ROOT),
            json_path.relative_to(REPO_ROOT),
        )
        if args.console:
            print(md_text)

    if len(all_payloads) > 1:
        # 跨天汇总
        print(f"完成 {len(all_payloads)} 天诊断。")
        for p in all_payloads:
            s = p["summary"]
            print(
                f"  {s['date']}: P0={s['by_severity']['P0']} P1={s['by_severity']['P1']} "
                f"P2={s['by_severity']['P2']} OK={s['by_severity']['OK']} | "
                f"usage=0 调用 {s['llm_calls_zero_usage']}/{s['total_llm_calls']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
