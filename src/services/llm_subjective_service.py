# -*- coding: utf-8 -*-
"""
LLM Subjective Scoring Service for P2.

Uses LLM to perform subjective analysis and scoring for:
1. Supply chain positioning
2. Value assessment
3. Sentiment analysis
"""

import json
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

from src.llm.concurrency import limited_acompletion
from src.config import get_config
from src.llm.generation_params import apply_minimax_reasoning_params

logger = logging.getLogger(__name__)


class LLMSubjectiveScoringService:
    """
    Service for LLM-driven subjective scoring.

    Uses LiteLLM to call LLM API for:
    - Supply chain positioning analysis
    - Value scenario analysis
    - Sentiment inference
    """

    def __init__(self, model: Optional[str] = None):
        self.model = model
        self._client = None

    def _get_client(self):
        """Lazy initialize LLM client"""
        if self._client is None:
            try:
                from litellm import acompletion

                self._client = acompletion
                logger.info("[LLMSubjectiveScoring] LiteLLM client initialized")
            except ImportError:
                logger.warning("[LLMSubjectiveScoring] LiteLLM not installed")
                self._client = None
        return self._client

    async def analyze_supply_chain(
        self,
        stock_code: str,
        stock_name: str,
        industry: str = "",
        main_business: str = "",
        concept_boards: Optional[List[Dict[str, Any]]] = None,
        institutional_data: Optional[Dict[str, Any]] = None,
        northbound_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze supply chain positioning using LLM.

        Args:
            stock_code: Stock code
            stock_name: Stock name
            industry: Industry classification
            main_business: Main business description
            concept_boards: Concept board data from provider
            institutional_data: Institutional holdings data
            northbound_data: Northbound flow data

        Returns:
            Dict with supply chain analysis results
        """
        from src.agents.prompts.supply_chain_prompts import build_supply_chain_prompt

        prompt = build_supply_chain_prompt(
            stock_code=stock_code,
            stock_name=stock_name,
            industry=industry,
            main_business=main_business,
            concept_boards=concept_boards,
            institutional_data=institutional_data,
            northbound_data=northbound_data,
        )

        try:
            response = await self._call_llm(prompt)
            return self._parse_supply_chain_response(response)
        except Exception as e:
            logger.error(f"[LLMSubjectiveScoring] Supply chain analysis failed: {e}")
            return self._get_fallback_supply_chain()

    async def analyze_value_scenario(
        self,
        stock_code: str,
        stock_name: str,
        pe: Optional[float] = None,
        pb: Optional[float] = None,
        roe: Optional[float] = None,
        revenue_growth: Optional[float] = None,
        sector: str = "",
    ) -> Dict[str, Any]:
        """
        Analyze value scenarios using LLM.

        Args:
            stock_code: Stock code
            stock_name: Stock name
            pe: P/E ratio
            pb: P/B ratio
            roe: Return on equity
            revenue_growth: Revenue growth rate
            sector: Industry sector

        Returns:
            Dict with value scenario analysis
        """
        prompt = self._build_value_prompt(
            stock_code,
            stock_name,
            pe if pe is not None else 0.0,
            pb if pb is not None else 0.0,
            roe if roe is not None else 0.0,
            revenue_growth if revenue_growth is not None else 0.0,
            sector,
        )

        try:
            response = await self._call_llm(prompt)
            return self._parse_value_response(response)
        except Exception as e:
            logger.error(f"[LLMSubjectiveScoring] Value analysis failed: {e}")
            return self._get_fallback_value()

    async def infer_sentiment(
        self,
        news_summary: str = "",
        analyst_report: str = "",
        social_sentiment: str = "",
    ) -> Dict[str, Any]:
        """
        Infer sentiment using LLM.

        Args:
            news_summary: News summary text
            analyst_report: Analyst report summary
            social_sentiment: Social media sentiment

        Returns:
            Dict with sentiment inference
        """
        prompt = self._build_sentiment_prompt(
            news_summary, analyst_report, social_sentiment
        )

        try:
            response = await self._call_llm(prompt)
            return self._parse_sentiment_response(response)
        except Exception as e:
            logger.error(f"[LLMSubjectiveScoring] Sentiment inference failed: {e}")
            return {"sentiment": "neutral", "confidence": 0.5}

    async def _call_llm(self, prompt: str) -> str:
        """Call LLM API"""
        client = self._get_client()
        if client is None:
            raise RuntimeError("LLM client not available")

        try:
            model = self.model or "gpt-3.5-turbo"
            call_kwargs = apply_minimax_reasoning_params(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
                [model],
                get_config().minimax_reasoning_effort,
            )
            response = await limited_acompletion(client, **call_kwargs)
            response_obj: Any = response
            content_raw: Any = response_obj.choices[0].message.content
            return content_raw if content_raw is not None else ""
        except Exception as e:
            logger.error(f"[LLMSubjectiveScoring] LLM call failed: {e}")
            raise

    def _parse_supply_chain_response(self, response: str) -> Dict[str, Any]:
        """Parse LLM supply chain response"""
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)

            return {
                "chain_position": data.get("chain_position", "midstream"),
                "chain_position_rationale": data.get("chain_position_rationale", ""),
                "moat_type": data.get("moat_type", "brand"),
                "moat_strength": data.get("moat_strength", "moderate"),
                "moat_rationale": data.get("moat_rationale", ""),
                "customer_concentration_hhi": data.get(
                    "customer_concentration_hhi", 0.25
                ),
                "us_china_risk": data.get("us_china_risk", "low"),
                "us_china_risk_rationale": data.get("us_china_risk_rationale", ""),
                "chokepoint_type": data.get("chokepoint_type", "none"),
                "overall_supply_chain_score": data.get(
                    "overall_supply_chain_score", 50
                ),
                "key_insights": data.get("key_insights", []),
                "risks": data.get("risks", []),
                "raw_response": response,
            }
        except Exception as e:
            logger.warning(f"[LLMSubjectiveScoring] Parse failed: {e}")
            return self._get_fallback_supply_chain()

    def _parse_value_response(self, response: str) -> Dict[str, Any]:
        """Parse LLM value analysis response"""
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)

            return {
                "value_anchor_1y": data.get("value_anchor_1y", ""),
                "value_anchor_3y": data.get("value_anchor_3y", ""),
                "value_anchor_5y": data.get("value_anchor_5y", ""),
                "scenario_analysis": data.get("scenario_analysis", {}),
                "value_score": data.get("value_score", 50),
                "raw_response": response,
            }
        except Exception as e:
            logger.warning(f"[LLMSubjectiveScoring] Value parse failed: {e}")
            return self._get_fallback_value()

    def _parse_sentiment_response(self, response: str) -> Dict[str, Any]:
        """Parse LLM sentiment response"""
        try:
            json_str = self._extract_json(response)
            data = json.loads(json_str)

            sentiment = data.get("sentiment", "neutral")
            if sentiment not in ("positive", "negative", "neutral"):
                sentiment = "neutral"

            return {
                "sentiment": sentiment,
                "confidence": min(1.0, max(0.0, data.get("confidence", 0.5))),
                "reasoning": data.get("reasoning", ""),
                "raw_response": response,
            }
        except Exception as e:
            logger.warning(f"[LLMSubjectiveScoring] Sentiment parse failed: {e}")
            return {"sentiment": "neutral", "confidence": 0.5}

    def _extract_json(self, text: str) -> str:
        """Extract JSON from LLM response"""
        import re

        text = text.strip()

        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines)

        json_match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
        if json_match:
            return json_match.group(0)

        raise ValueError("No JSON found in response")

    def _build_value_prompt(
        self,
        stock_code: str,
        stock_name: str,
        pe: float,
        pb: float,
        roe: float,
        revenue_growth: float,
        sector: str,
    ) -> str:
        """Build value analysis prompt"""
        return f"""分析{stock_name}({stock_code})的价值情景。

基本面数据:
- 市盈率(PE): {pe or "未知"}
- 市净率(PB): {pb or "未知"}
- 净资产收益率(ROE): {roe or "未知"}%
- 营收增长率: {revenue_growth or "未知"}%
- 行业: {sector or "未知"}

请输出JSON格式的价值分析:
```json
{{
    "value_anchor_1y": "1年价值锚(如: 150-180元)",
    "value_anchor_3y": "3年价值锚",
    "value_anchor_5y": "5年价值锚",
    "scenario_analysis": {{
        "bull_case_upside": "乐观情景上涨空间(%)",
        "base_case_upside": "基准情景上涨空间(%)",
        "bear_case_downside": "悲观情景下跌空间(%)"
    }},
    "value_score": 0-100
}}
```
"""

    def _build_sentiment_prompt(
        self,
        news_summary: str,
        analyst_report: str,
        social_sentiment: str,
    ) -> str:
        """Build sentiment inference prompt"""
        return f"""分析以下信息的市场情绪:

新闻摘要: {news_summary or "暂无"}
券商研报: {analyst_report or "暂无"}
社交媒体: {social_sentiment or "暂无"}

请输出JSON格式的情绪分析:
```json
{{
    "sentiment": "positive|negative|neutral",
    "confidence": 0.0-1.0,
    "reasoning": "判断理由(不超过50字)"
}}
```
"""

    def _get_fallback_supply_chain(self) -> Dict[str, Any]:
        """Fallback when LLM fails"""
        return {
            "chain_position": "midstream",
            "chain_position_rationale": "LLM分析失败，使用默认值",
            "moat_type": "brand",
            "moat_strength": "moderate",
            "moat_rationale": "LLM分析失败，使用默认值",
            "customer_concentration_hhi": 0.25,
            "us_china_risk": "medium",
            "us_china_risk_rationale": "LLM分析失败，使用默认值",
            "chokepoint_type": "none",
            "overall_supply_chain_score": 50,
            "key_insights": ["LLM分析暂不可用"],
            "risks": ["数据不足"],
            "raw_response": None,
        }

    def _get_fallback_value(self) -> Dict[str, Any]:
        """Fallback when LLM fails"""
        return {
            "value_anchor_1y": "待分析",
            "value_anchor_3y": "待分析",
            "value_anchor_5y": "待分析",
            "scenario_analysis": {},
            "value_score": 50,
            "raw_response": None,
        }


_llm_service = None


def get_llm_subjective_service() -> LLMSubjectiveScoringService:
    """Get singleton LLM subjective scoring service"""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMSubjectiveScoringService()
    return _llm_service
