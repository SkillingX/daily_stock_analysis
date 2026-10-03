# -*- coding: utf-8 -*-
"""产业链研究员——独立研究员 Skill（SKILL.md + 本模块代码）。

代码自治：本模块持有该员的工具子集/契约解析/Skill 定义；探索循环为全研究员共享基建。
"""

from __future__ import annotations

from src.agent.deep_research.explore_agents import run_supply_chain_agent
from src.deep_research.researchers.base import generic_parse

DIM_ID = "supply_chain"
DISPLAY_NAME = "产业链研究员"
SKILL_MD = "researchers/supply_chain/SKILL.md"
TOOLS = frozenset(frozenset({'get_sector_rankings', 'search_comprehensive_intel', 'get_stock_info', 'verify_supply_chain_evidence'}))
TTL_HOURS = 120.0
SCORE_KEY = None

def runner_fn(stock_code, stock_name, llm_adapter, progress_callback=None, max_steps=8):
    """产业链研究员：已有该股票的供应链专项报告则直接复用（用户决策）；无则探索兜底。"""
    try:
        from src.storage import get_db

        record = get_db().get_latest_supply_chain_report_by_stock(stock_code)
        if record is not None:
            deep = {}
            if record.deep_dive_json:
                import json as _json

                try:
                    deep = _json.loads(record.deep_dive_json)
                except ValueError:
                    deep = {}
            return {
                "ok": True,
                "data": {
                    "existing_report": {
                        "report_id": record.id,
                        "topic": record.topic or "",
                        "created_at": record.created_at.isoformat()[:10] if record.created_at else "",
                        "link": f"/api/v1/supply-chain/reports/{record.id}",
                        "deep_dive": deep,
                    }
                },
                "steps": 0,
            }
    except Exception:  # noqa: BLE001 - 查询失败不阻断，退回探索
        pass
    return run_supply_chain_agent(
        stock_code, stock_name, llm_adapter, progress_callback, max_steps
    )


def parse(parsed, steps):
    existing = (parsed or {}).get("existing_report")
    if existing:
        return _parse_existing(existing), steps
    return generic_parse(DIM_ID, parsed, steps)


def _parse_existing(existing):
    """供应链专项报告复用：链接+日期+深潜结构映射进契约。"""
    from src.schemas.supply_chain import SupplyChain

    deep = existing.get("deep_dive") or {}
    model = SupplyChain(
        company_position=(
            f"详见供应链专项报告「{existing.get('topic') or '产业链分析'}」"
            f"（{existing.get('created_at') or '日期待考'}生成）"
        ),
        chokepoints=list(deep.get("chokepoints") or []),
        upstream=list(deep.get("upstream") or []),
        downstream=list(deep.get("downstream") or []),
    )
    from src.schemas.deep_research_dims import SupplyChainDim

    return SupplyChainDim(
        supply_chain=model,
        verification_status=f"existing_report:{existing.get('report_id')}",
        narrative=(
            f"本维度直接复用供应链模块专项报告（{existing.get('created_at')}）："
            f"{existing.get('link')} —— 含产业链图谱/瓶颈/议价力与双源校验差异。"
        ),
    )
