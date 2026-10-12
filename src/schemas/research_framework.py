# -*- coding: utf-8 -*-
"""
Research Framework Schema.

Defines data structures for the six-dimension research framework.
"""

from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class IndicatorScore(BaseModel):
    """Single indicator score"""

    name: str = Field(..., description="Indicator name")
    score: float = Field(..., ge=0, le=100, description="Score 0-100")
    weight: float = Field(..., ge=0, le=1, description="Weight within dimension")
    basis: Literal["rule", "llm"] = Field("rule", description="Scoring basis")
    confidence: Optional[Literal["high", "medium", "low"]] = Field(
        None, description="Data confidence"
    )
    summary: Optional[str] = Field(None, description="Score reasoning")
    data_gap: bool = Field(False, strict=True, description="Missing or ineligible inputs use the neutral placeholder")


class DimensionScore(BaseModel):
    """Single dimension score"""

    dimension: str = Field(..., description="Dimension name")
    weight: float = Field(..., ge=0, le=1, description="Dimension weight")
    score: float = Field(..., ge=0, le=100, description="Dimension score")
    indicators: List[IndicatorScore] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list, description="Warning messages")


class ResearchFramework(BaseModel):
    """Six-dimension research framework"""

    dimension_total: float = Field(
        ..., ge=0, le=100, description="Six-dimension total score"
    )
    dimensions: List[DimensionScore] = Field(default_factory=list)
    scoring_version: str = Field("v1", description="Scoring version")
    warnings: List[str] = Field(default_factory=list)
