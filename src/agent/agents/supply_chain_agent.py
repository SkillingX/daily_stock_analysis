# -*- coding: utf-8 -*-
"""
SupplyChainAgent — supply chain positioning specialist.

Responsible for:
- Analyzing supply chain positioning (upstream, midstream, downstream, bottleneck)
- Assessing moat type and strength
- Evaluating US-China dual-chain risk
- Identifying chokepoints and customer concentration
- Producing a structured supply chain analysis opinion
"""

from __future__ import annotations

import logging
from typing import Optional

from src.agent.agents.base_agent import BaseAgent
from src.agent.protocols import AgentContext, AgentOpinion
from src.agent.runner import try_parse_json

logger = logging.getLogger(__name__)


class SupplyChainAgent(BaseAgent):
    agent_name = "supply_chain"
    max_steps = 5
    tool_names = [
        "get_stock_info",
        "get_institutional_holdings",
        "get_northbound_flow",
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        return """\
You are a **Supply Chain Positioning Analyst** specialising in A-shares, \
HK, and US equities.

Your task: analyze the stock's position in the supply chain, assess its \
competitive moat, and identify key risks.

## Workflow
1. Call get_stock_info to get basic company information (industry, main business)
2. Read concept membership from get_stock_info (iFinD latest concepts; labels are not customer facts)
3. Call get_institutional_holdings to check major institutional holders and their changes
4. Call get_northbound_flow to check foreign capital flow trends
5. Analyze supply chain positioning and produce a structured JSON opinion

## Supply Chain Position Classification
- bottleneck (卡脖子): Core technology/materials, irreplaceable - highest strategic value
- upstream (上游): Raw materials or core component suppliers
- midstream (中游): Processing and manufacturing links
- downstream (下游): End products/services
- commodity (大宗商品): Highly homogeneous competition

## Moat Type Classification
- patent: Patent-protected technology or IP
- technology: Superior technical capabilities
- brand: Strong brand recognition
- network: Network effects (users, data, ecosystem)
- switching_cost: High customer switching costs
- license: Regulatory licenses or concessions
- regulatory: Regulatory barriers to entry
- multiple: Multiple moat types combined

## Moat Strength Assessment
- strong: Obvious patent/tech/brand/network effects
- moderate: Some barriers but can be replicated
- weak: Weak barriers
- none: No moat

## US-China Dual Chain Risk
- high: Highly dependent on US or Chinese supply chains
- medium: Some exposure, manageable
- low: Limited exposure
- none: Fully domestic operation

## Chokepoint Types
- patent: Key patents that block competitors
- capacity: Dominant capacity in a critical环节
- geo: Geographic advantages in key regions
- tech: Core technology advantages
- cert: Required certifications (FDA, etc.)
- network: Network effect chokepoints
- none: No obvious chokepoints

## Customer Concentration (HHI)
- HHI < 0.15: Diversified customer base
- 0.15 <= HHI < 0.25: Moderate concentration
- HHI >= 0.25: High concentration risk

## Output Format
Return **only** a JSON object:
{
  "chain_position": "upstream|bottleneck|midstream|downstream|commodity",
  "chain_position_rationale": "Brief explanation of position (max 100 chars)",
  "moat_type": "patent|technology|brand|network|switching_cost|license|regulatory|multiple",
  "moat_strength": "strong|moderate|weak|none",
  "moat_rationale": "Moat analysis explanation",
  "customer_concentration_hhi": 0.0-1.0,
  "customer_concentration_rationale": "Customer concentration analysis",
  "us_china_risk": "high|medium|low|none",
  "us_china_risk_rationale": "US-China dual chain risk analysis",
  "chokepoint_type": "patent|capacity|geo|tech|cert|network|none",
  "chokepoint_rationale": "Chokepoint analysis if applicable",
  "overall_supply_chain_score": 0-100,
  "key_insights": ["insight1", "insight2", "insight3"],
  "risks": ["risk1", "risk2", "risk3"],
  "signal": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": 0.0-1.0,
  "reasoning": "Overall supply chain analysis summary"
}
"""

    def build_user_message(self, ctx: AgentContext) -> str:
        parts = [f"Analyze supply chain positioning for stock **{ctx.stock_code}**"]
        if ctx.stock_name:
            parts[0] += f" ({ctx.stock_name})"
        parts.append(
            "Steps:\n"
            "1. Call get_stock_info to get industry and main business info.\n"
            "2. Read concept membership from get_stock_info (iFinD latest concepts).\n"
            "3. Call get_institutional_holdings to check major holders.\n"
            "4. Call get_northbound_flow to check foreign capital flow.\n"
            "5. Output the JSON opinion including supply chain positioning analysis."
        )
        return "\n".join(parts)

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        parsed = try_parse_json(raw_text)
        if parsed is None:
            logger.warning("[SupplyChainAgent] failed to parse opinion JSON")
            return None

        ctx.set_data("supply_chain_opinion", parsed)

        signal = parsed.get("signal", "hold")
        if signal not in ("strong_buy", "buy", "hold", "sell", "strong_sell"):
            signal = "hold"

        confidence = float(parsed.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        reasoning = parsed.get("reasoning", "")
        key_insights = parsed.get("key_insights", [])
        if isinstance(key_insights, list) and reasoning:
            reasoning = (
                reasoning
                + "\n\n关键洞察:\n"
                + "\n".join(f"- {i}" for i in key_insights[:3])
            )

        return AgentOpinion(
            agent_name=self.agent_name,
            signal=signal,
            confidence=confidence,
            reasoning=reasoning,
            raw_data=parsed,
        )
