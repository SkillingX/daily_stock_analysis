# -*- coding: utf-8 -*-
"""iFinD 最新概念归属适配（issue #45）。

确定性反例：公开概念探针（东财/同花顺）一旦被调用即失败；
概念归属只证明最新时点，不补造日期、不混用行业分类。
"""
from __future__ import annotations

import json

import pytest

from data_provider.cross_source_validator import SourceReadError
from data_provider.ifind_fundamental_adapter import (
    ConceptMembership,
    IfindFetcher,
    _parse_concepts,
    IfindData,
)


def _wire(answer: str) -> IfindData:
    return IfindData(answer=answer, indicators_params={"所属概念": {"截止日期": "最新"}})


class TestParseConcepts:
    def test_real_300260_shape(self) -> None:
        answer = (
            "|证券代码|证券简称|所属概念|\n|---|---|---|\n"
            "|300260.SZ|新莱应材|食品安全,海峡两岸,芯片概念,深股通|\n"
        )
        membership = _parse_concepts(_wire(answer), "300260")
        assert membership.concepts == ["食品安全", "海峡两岸", "芯片概念", "深股通"]
        assert membership.source == "ifind"
        assert membership.as_of_nature == "latest"
        assert not membership.errors

    def test_security_mismatch_is_distinct(self) -> None:
        answer = "|证券代码|所属概念|\n|---|---|\n|600519.SH|白酒概念|\n"
        membership = _parse_concepts(_wire(answer), "300260")
        assert membership.concepts == []
        assert membership.errors == ["security_mismatch"]

    def test_empty_cell_is_no_concepts(self) -> None:
        answer = "|证券代码|所属概念|\n|---|---|\n|300260.SZ| |\n"
        membership = _parse_concepts(_wire(answer), "300260")
        assert membership.concepts == []
        assert membership.errors == ["no_concepts"]

    def test_dedup_and_trim(self) -> None:
        answer = "|证券代码|所属概念|\n|---|---|\n|300260.SZ|芯片概念, 芯片概念,深股通|\n"
        membership = _parse_concepts(_wire(answer), "300260")
        assert membership.concepts == ["芯片概念", "深股通"]


class _FakeFetcher(IfindFetcher):
    def __init__(self, reply: object) -> None:
        super().__init__(endpoint="https://fixture.invalid", token="fixture")
        self._reply = reply

    def query(self, tool: str, question: str, *, deadline: float | None = None) -> object:
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


class TestConceptMembershipEntry:
    def test_success_through_adapter(self) -> None:
        answer = "|证券代码|所属概念|\n|---|---|\n|300260.SZ|液冷服务器,芯片设备|\n"
        fetcher = _FakeFetcher(_wire(answer))
        membership = fetcher.concept_membership("300260", deadline=10**9)
        assert isinstance(membership, ConceptMembership)
        assert membership.concepts == ["液冷服务器", "芯片设备"]

    @pytest.mark.parametrize("reason", ["authentication_failed", "quota_exhausted", "timeout"])
    def test_supplier_failures_are_distinct(self, reason: str) -> None:
        fetcher = _FakeFetcher(SourceReadError("ifind", reason))
        with pytest.raises(SourceReadError) as exc:
            fetcher.concept_membership("300260", deadline=10**9)
        assert exc.value.reason_code == reason


class TestPublicProbeRetirement:
    """退休探针一旦被生产路径触达即失败（确定性反例）。"""

    def test_default_validator_removed(self) -> None:
        import data_provider.supply_chain.cross_source as cross_source

        assert not hasattr(cross_source, "get_default_validator")
        assert not hasattr(cross_source, "EastMoneyProbe")
        assert not hasattr(cross_source, "ThsAkshareProbe")

    def test_tool_registry_has_no_probe_tool(self) -> None:
        from src.agent.tools.supply_chain_tools import ALL_SUPPLY_CHAIN_TOOLS

        assert "verify_supply_chain_evidence" not in {tool.name for tool in ALL_SUPPLY_CHAIN_TOOLS}

    def test_researcher_tool_sets_exclude_probe(self) -> None:
        from src.deep_research.researchers.supply_chain import TOOLS as supply_tools
        from src.deep_research.researchers.us_china import TOOLS as us_china_tools

        assert "verify_supply_chain_evidence" not in supply_tools
        assert "verify_supply_chain_evidence" not in us_china_tools
        assert "get_stock_info" in supply_tools and "get_stock_info" in us_china_tools

    def test_model_requesting_retired_tool_cannot_execute(self) -> None:
        """模型仍请求旧工具时不能执行，也不能被别名解析回探针。"""
        from src.agent.factory import get_tool_registry

        registry = get_tool_registry()
        assert registry.resolve("verify_supply_chain_evidence") is None
        with pytest.raises(KeyError):
            registry.execute("verify_supply_chain_evidence", stock_code="300260")
