# -*- coding: utf-8 -*-
"""Issue #64 范围兼容：18 项有效集合、历史 19 项识别、仅退役 400、未知 422。

覆盖共享解析函数、API 受理边界（取数前）、服务/编排直调边界与默认闭包。
全部离线：模型/数据服务层被替换为记录参数的桩，真实出站零发生。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.endpoints.deep_research import router
from src.schemas.deep_research_dims import DIM_IDS


# ---------------------------------------------------------------------------
# 共享集合与解析函数
# ---------------------------------------------------------------------------


def test_active_dim_ids_excludes_retired() -> None:
    from src.schemas.deep_research_dims import ACTIVE_DIM_IDS, RETIRED_DIM_IDS

    assert "ownership" in DIM_IDS, "历史 19 项目录必须保留 ownership 供旧报告解析"
    assert RETIRED_DIM_IDS == ("ownership",)
    assert "ownership" not in ACTIVE_DIM_IDS
    assert len(ACTIVE_DIM_IDS) == 18
    assert len(DIM_IDS) == 19


def test_resolve_selection_none_and_empty_mean_default_18() -> None:
    from src.schemas.deep_research_dims import ACTIVE_DIM_IDS, resolve_dim_selection

    assert resolve_dim_selection(None) is None
    assert resolve_dim_selection([]) is None
    # 默认全量 = 当前 18 项（expand 层把 None 展开为 ACTIVE 集合）
    from src.agent.deep_research.orchestrator import expand_dim_selection

    assert expand_dim_selection(None) == set(ACTIVE_DIM_IDS)
    assert expand_dim_selection(set()) == set(ACTIVE_DIM_IDS)


def test_resolve_selection_mixed_and_full19_drop_retired() -> None:
    from src.schemas.deep_research_dims import ACTIVE_DIM_IDS, resolve_dim_selection

    full = resolve_dim_selection(list(DIM_IDS))
    assert full is not None and "ownership" not in full
    assert full == set(ACTIVE_DIM_IDS)
    mixed = resolve_dim_selection(["ownership", "capital"])
    assert mixed == {"capital"}


def test_resolve_selection_unknown_beats_retired() -> None:
    from src.schemas.deep_research_dims import RetiredDimRequestError, resolve_dim_selection

    with pytest.raises(ValueError) as unknown_only:
        resolve_dim_selection(["no_such_dim"])
    assert "未知分析维度" in str(unknown_only.value)
    # 未知 ID 先校验：未知 + 退役 → 422 语义，不是 400
    with pytest.raises(ValueError) as mixed_unknown:
        resolve_dim_selection(["ownership", "no_such_dim"])
    assert not isinstance(mixed_unknown.value, RetiredDimRequestError)
    with pytest.raises(RetiredDimRequestError):
        resolve_dim_selection(["ownership"])


def test_expand_selection_subset_closure_has_no_retired() -> None:
    from src.agent.deep_research.orchestrator import expand_dim_selection

    expanded = expand_dim_selection({"signal"})
    assert "ownership" not in expanded
    assert {"signal", "conclusion", "scenarios", "six_dim", "bayesian", "data"} <= expanded


def test_expand_selection_rejects_unknown_before_any_io() -> None:
    """直接编排边界与 API/服务同决策：未知 ID 拒绝，不带着未知项启动取数。"""
    from src.agent.deep_research.orchestrator import expand_dim_selection
    from src.schemas.deep_research_dims import RetiredDimRequestError

    with pytest.raises(ValueError) as unknown:
        expand_dim_selection({"ownership", "no_such_dim"})
    assert not isinstance(unknown.value, RetiredDimRequestError)
    assert "未知分析维度" in str(unknown.value)
    with pytest.raises(RetiredDimRequestError):
        expand_dim_selection({"ownership"})


# ---------------------------------------------------------------------------
# API 受理边界（校验在 I/O 前；服务层替换为记录桩）
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_app(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    from src.config import get_config
    from src.services import deep_research_service as service

    calls: Dict[str, Any] = {"dims": None, "count": 0}

    def fake_generate(self: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        calls["count"] += 1
        calls["dims"] = kwargs.get("dims")
        return {
            "report_id": "600519_20261029000000", "status": "success", "quality_score": 100,
            "missing_layers": [], "markdown": "# 报告", "error": None, "run_id": None,
            "end_reason": None, "stock_code": "600519", "stock_name": "样本",
            "md_path": None, "total_steps": 0, "total_tokens": 0, "provider": "",
        }

    # 统一类级 patch：实例级 patch 的 teardown 会把绑定方法固化为实例属性，
    # 遮蔽后续类级 mock 并放行真实生成（R6 根因）。Config 同理走类级。
    monkeypatch.setattr(service.DeepResearchService, "generate_report", fake_generate)
    from src.config import Config

    monkeypatch.setattr(Config, "is_agent_available", lambda self: True)
    app = FastAPI()
    app.include_router(router)
    return {"app": app, "calls": calls}


@pytest.fixture(autouse=True)
def _block_outbound_sockets(monkeypatch: pytest.MonkeyPatch):
    """任何真实出站 socket connect 一律失败（ASGI/TestClient 走进程内传输不受影响）。"""
    import socket

    def no_connect(self: Any, address: Any) -> None:
        raise AssertionError(f"unexpected outbound socket connect: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", no_connect)


@pytest.fixture(autouse=True)
def _isolated_report_storage(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """报告/维度产物落盘隔离到 tmp（纵深防御：mock 失效也不写真实报告目录）。"""
    from pathlib import Path

    from src.services import deep_research_service as service

    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(service, "_DEEP_RESEARCH_DIR", reports)
    monkeypatch.setattr(service, "_REPORTS_ROOT", tmp_path)
    yield reports


@pytest.fixture(autouse=True)
def _credential_free_choice_config(monkeypatch: pytest.MonkeyPatch):
    """文件级无凭据确定性配置（协调者实测口径）：fresh Config(choice) + 禁 iFinD，
    不读任何私有 .env；需要其他配置的用例自行类级覆盖。"""
    from src.config import Config

    config = Config(data_source_profile="choice", enable_ifind_mcp=False, deep_research_cross_validate=False)
    monkeypatch.setattr(Config, "_instance", config)
    yield config


def _post(client: TestClient, dims: Optional[List[str]]) -> Any:
    body: Dict[str, Any] = {"stock_code": "600519", "stock_name": "样本", "force_refresh": True}
    if dims is not None:
        body["dims"] = dims
    return client.post("/generate/stream", json=body)


def test_api_retired_only_rejected_400_before_io(api_app: Dict[str, Any]) -> None:
    from src.core.market_policy import require_stock_market

    require_stock_market("600519")
    with TestClient(api_app["app"]) as client:
        response = _post(client, ["ownership"])
    assert response.status_code == 400
    assert "本版已停用" in response.text
    assert api_app["calls"]["count"] == 0, "仅退役请求必须在受理/取数前拒绝，服务层零调用"


def test_api_unknown_dim_422(api_app: Dict[str, Any]) -> None:
    with TestClient(api_app["app"]) as client:
        response = _post(client, ["ownership", "no_such_dim"])
    assert response.status_code == 422
    assert api_app["calls"]["count"] == 0, "未知 ID 在受理层拒绝，服务层零调用"


def test_api_mixed_and_default_start_with_dropped_scope(api_app: Dict[str, Any]) -> None:
    with TestClient(api_app["app"]) as client:
        mixed = _post(client, ["ownership", "capital"])
        assert mixed.status_code == 200, mixed.text
        assert api_app["calls"]["dims"] == ["capital"], "混合请求剔除退役 ID 后受理"
        default = _post(client, None)
        assert default.status_code == 200, default.text
        assert api_app["calls"]["dims"] is None, "默认请求保持 None 语义（全量 18）"


def test_api_full19_accepted_with_dropped_scope(api_app: Dict[str, Any]) -> None:
    with TestClient(api_app["app"]) as client:
        response = _post(client, list(DIM_IDS))
    assert response.status_code == 200, response.text
    sent = api_app["calls"]["dims"]
    assert sent is not None and "ownership" not in sent
    assert len(sent) == 18


# ---------------------------------------------------------------------------
# 既有公司原始事实保留（规格 §1：退役股权研究不丢已取得的 Choice 原始字段）
# ---------------------------------------------------------------------------


def test_business_runner_projects_choice_company_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """阶段0已取得的 AHOLDER/HOLDNAMER+报告期经既有 business 资料字段留存展示，
    不新取数、不推断控制链、不评分。"""
    from datetime import date

    from src.agent.deep_research import explore_agents
    from src.agent.tools import data_tools

    monkeypatch.setattr(
        explore_agents, "_run_explore",
        lambda *a, **kw: {"ok": True, "data": {"narrative": "业务画像结论。"}, "steps": 1},
    )
    monkeypatch.setattr(
        data_tools, "_handle_get_stock_info",
        lambda *a, **kw: {"fundamental_context": {"research": {
            "as_of": date.today().isoformat(),
            "company": {"AHOLDER": "实控人甲", "MAINBUSINESS": "制造设备"},
            "holders": {"HOLDNAMER": "股东甲;股东乙", "period": "2026-06-30"},
        }}},
    )
    from src.core.research_run import ResearchRun

    with ResearchRun(30).bind():
        out = explore_agents.run_business_agent("300260", "样本", None)
    assert out["ok"] is True
    narrative = str(out["data"].get("narrative") or "")
    assert "业务画像结论" in narrative
    assert "实控人甲" in narrative and "原始字段" in narrative
    assert "股东甲" in narrative and "股东乙" in narrative
    assert "2026-06-30" in narrative and "未核实控制链" in narrative
    # 模型结论字段不受影响：无治理评分复活
    assert not out["data"].get("score")


# ---------------------------------------------------------------------------
# 服务直调边界（CLI 等不经 HTTP 路由的消费者同决策）
# ---------------------------------------------------------------------------


def test_service_rejects_retired_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """仅退役请求在受理边界拒绝；类级 raise 桩证明零后续生成调用、零出站。"""
    from src.services.deep_research_service import DeepResearchInputError, DeepResearchService, deep_research_service

    def no_generation(self: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise AssertionError("受理边界拒绝后不得进入任何生成/取数路径")

    monkeypatch.setattr(DeepResearchService, "_generate_report", no_generation)
    with pytest.raises(DeepResearchInputError) as excinfo:
        deep_research_service.generate_report(
            raw_code="600519", raw_name="样本", dims=["ownership"],
            progress_callback=None, force_refresh=True,
            run=None,
        )
    assert "本版已停用" in str(excinfo.value)


def test_service_drops_retired_from_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services import deep_research_service as service

    captured: Dict[str, Any] = {}

    def fake_inner(self: Any, code: str, name: str, report_type: str, cb: Any, dims: Optional[List[str]], force_refresh: bool) -> Dict[str, Any]:
        captured["dims"] = dims
        return {"status": "success", "quality_score": 100, "missing_layers": [], "markdown": "m", "report_id": "x",
                "total_steps": 0, "total_tokens": 0, "provider": "", "error": None, "success": True}

    # 类级 patch（与文件内其余 service patch 统一，防实例属性遮蔽，见 R6）
    monkeypatch.setattr(service.DeepResearchService, "_generate_report", fake_inner)
    out = service.deep_research_service.generate_report(
        raw_code="600519", raw_name="样本", dims=["ownership", "capital"],
        progress_callback=None, force_refresh=True, run=None,
    )
    assert out["status"] == "success"
    assert captured["dims"] == ["capital"]
