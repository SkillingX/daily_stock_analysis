# -*- coding: utf-8 -*-
"""Schemas for LLM usage tracking API."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class CallTypeBreakdown(BaseModel):
    call_type: str = Field(..., description="'analysis' | 'agent' | 'market_review'")
    calls: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int


class ModelBreakdown(BaseModel):
    model: str
    calls: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int
    max_total_tokens: int = 0


class UsageCallRecord(BaseModel):
    id: int
    called_at: str = Field(..., description="ISO datetime string")
    call_type: str
    model: str
    stock_code: Optional[str] = None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    # Appended honesty fields (legacy numerics unchanged): which token fields
    # the provider did not report. A displayed 0 with unknown_fields naming it
    # is not confirmed zero consumption.
    completeness: Literal["known", "partial", "unknown"] = "known"
    unknown_fields: List[str] = Field(default_factory=list)


class UsageSummaryResponse(BaseModel):
    period: str = Field(..., description="'today' | 'month' | 'all'")
    from_date: str = Field(..., description="ISO date string")
    to_date: str = Field(..., description="ISO date string")
    total_calls: int
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int
    by_call_type: List[CallTypeBreakdown]
    by_model: List[ModelBreakdown]


class UsageDashboardResponse(UsageSummaryResponse):
    recent_calls: List[UsageCallRecord]
