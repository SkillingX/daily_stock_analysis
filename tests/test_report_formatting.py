"""Report display regressions: real templates and saved-report API responses.
Run: python -m pytest tests/test_report_formatting.py
No provider calls; historical files are isolated in pytest's temporary directory.
"""
import json
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.services import fundamentals_service as fundamentals
from src.services import financial_analysis_service as financial
from src.services import sector_analysis_service as sector


def test_business_fields_and_lists_are_readable() -> None:
    dims = {'business': {'business_model': {'value': '主营设备，value 是正文词'},
                        'main_products': {'products': ['设备', '配件']},
                        'competitive_position': {'industry_rank': '行业地位资料', 'moat': '认证壁垒'},
                        'vs_market_leader': {'value': '[公告](https://example.com)'}}}
    md = fundamentals._render_markdown('样本', '300260', 'fixture', dims)
    assert '<font' not in md
    assert '**value**' not in md and '**industry_rank**' not in md
    assert 'value 是正文词' in md and '[公告](https://example.com)' in md
    assert '- **行业地位**：行业地位资料\n- **护城河**：认证壁垒' in md
    assert "['设备'" not in md and '- 设备' in md


def test_financial_and_sector_templates_emit_markdown() -> None:
    md = financial._render('样本', '300260', 'fixture', 'fixture', financial._score_dims({'years': []}, {}))
    assert '<font' not in md and '**盈利质量**' in md
    data: dict[str, Any] = {
        'weights': {'policy': .3, 'base_rate': .3, 'prosperity': .4},
        'identification': {'sw_chain': {}}, 'policy': {},
        'base_rate': {'base_rate': .5, 'percentile': None, 'table_version': 'v1', 'all_rates': []},
        'prosperity': {'sector_found': False},
    }
    md = sector._render('样本', '300260', 'fixture', 'fixture', data)
    assert '<font' not in md and '🔴 **基率表版本' in md


def test_legacy_fundamentals_read_and_download_agree_without_rewriting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from api.v1.endpoints import fundamentals as api
    raw = '> 报告日期：2026-10-12T10:00:00 ｜ AI 生成\n\n- <font color="#e03131">🔴 **value**</font>：旧业务\n'
    path = tmp_path / 'old.md'
    path.write_text(raw)
    from src.schemas.deep_research_dims import BusinessDim, FundamentalDim, SectorDim
    record = {'id': 'fd_202610121000', 'stock_code': '300260', 'stock_name': '样本',
              'created_at': '2026-10-12T09:59:00', 'md_path': str(path),
              'dims_json': json.dumps({'business': BusinessDim(business_model={'value': '旧业务'}, data_gaps=['旧缺口']).model_dump(), 'sector': SectorDim().model_dump(), 'financial': FundamentalDim().model_dump()})}
    monkeypatch.setattr(api, 'get_db', lambda: SimpleNamespace(get_fundamentals_report=lambda _: dict(record)))
    app = FastAPI()
    app.include_router(api.router, prefix='/api/v1/fundamentals')
    with TestClient(app) as client:
        md = client.get('/api/v1/fundamentals/reports/'+record['id']).json()['data']['markdown']
        download = client.get('/api/v1/fundamentals/reports/'+record['id']+'/markdown?download=1')
    assert md == download.text
    assert '<font' not in md and '**value**' not in md
    assert '旧业务' in md and '旧缺口' in md and '财务数据未核验' in md
    assert '报告日期：2026-10-12T10:00:00' in md
    assert path.read_text() == raw and 'font' in raw


@pytest.mark.parametrize("dims", [{}, {"business": {}, "sector": {}, "financial": {}}, {"business": {"status": "ok"}, "sector": {"status": "ok"}, "financial": {"status": "ok"}}])
def test_incomplete_history_keeps_original_content(dims: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from api.v1.endpoints import fundamentals as api
    raw = '> 报告日期：2026-10-12T10:00:00 ｜ AI 生成\n<font color="#e03131">已保存的业务事实</font>'
    path = tmp_path / 'incomplete.md'
    path.write_text(raw)
    record = {'id': 'fd_202610121000', 'stock_code': '300260', 'stock_name': '样本', 'md_path': str(path), 'dims_json': json.dumps(dims)}
    monkeypatch.setattr(api, 'get_db', lambda: SimpleNamespace(get_fundamentals_report=lambda _: dict(record)))
    app = FastAPI()
    app.include_router(api.router, prefix='/api/v1/fundamentals')
    with TestClient(app) as client:
        md = client.get('/api/v1/fundamentals/reports/'+record['id']).json()['data']['markdown']
        download = client.get('/api/v1/fundamentals/reports/'+record['id']+'/markdown').text
    assert raw in md and md == download
    assert path.read_text() == raw
