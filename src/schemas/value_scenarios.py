# -*- coding: utf-8 -*-
"""
Value Scenarios Schema.

Defines data structures for long-term value scenarios.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, List, Optional, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnalystTarget(BaseModel):
    """Dated vendor research consensus; not a probability-weighted fair value."""
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, allow_inf_nan=False)
    source: Literal["choice"] = "choice"
    indicator: Literal["RATINGTARGETPRICE"] = "RATINGTARGETPRICE"
    as_of: date
    window_days: Literal[180] = 180
    currency: Literal["CNY"] = "CNY"
    price: Annotated[Decimal, Field(gt=0)]
    low: Annotated[Decimal, Field(gt=0)] | None = None
    high: Annotated[Decimal, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def consistent_reference(self) -> "AnalystTarget":
        if self.as_of > date.today():
            raise ValueError("Target reference date cannot be in the future")
        if self.low is not None and self.low > self.price or self.high is not None and self.high < self.price:
            raise ValueError("Consensus must lie within published target bounds")
        return self


class Scenario(BaseModel):
    """Scenario"""

    type: Literal["optimistic", "neutral", "pessimistic"] = Field(...)
    probability: float = Field(..., ge=0, le=1)
    value_anchor: Optional[float] = Field(None, description="Value anchor price")
    description: Optional[str] = Field(None)


class ValueHorizons(BaseModel):
    """Value horizons"""

    horizon_1y: Optional[str] = Field(None, description="1-year value range")
    horizon_3y: Optional[str] = Field(None, description="3-year value range")
    horizon_5y: Optional[str] = Field(None, description="5-year value range")


class ValueScenarios(BaseModel):
    """Long-term value and scenarios"""

    industry_space: Optional[str] = Field(None, description="Industry space/size")
    competitive_evolution: Optional[str] = Field(
        None, description="Competitive landscape evolution"
    )
    scenarios: List[Scenario] = Field(default_factory=list)
    horizons: Optional[ValueHorizons] = Field(None)
    catalysts: List[str] = Field(default_factory=list, description="Future catalysts")
    risks: List[str] = Field(default_factory=list, description="Long-term risks")
