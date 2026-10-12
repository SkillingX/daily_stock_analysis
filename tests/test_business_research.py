"""Verify focused business evidence from search through the report API.
Run: python -m pytest tests/test_business_research.py
Uses synthetic search/model providers and an isolated financial fixture.
"""
from datetime import date
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.search_service import SearchResponse, SearchResult, SearchService
from tests.test_fundamentals_data_quality import financial_sources  # noqa: F401


class EvidenceProvider:
    name = 'fixture'
    is_available = True
    def __init__(self) -> None:
        self.queries: list[str] = []
    def search(self, query: str, **kwargs: Any) -> SearchResponse:
        self.queries.append(query)
        return SearchResponse(query=query, provider=self.name, results=[
            SearchResult(title='样本 600519 历史主营业务', snippet='已过期材料', url='https://example.org/old', source='fixture', published_date='2020-01-01'),
            SearchResult(title='样本 600519 主营业务公告', snippet='公司主营设备及配件，收入结构未披露。', url='https://example.org/disclosure', source='fixture', published_date=None),
        ])


def test_business_search_focuses_on_three_questions_without_news() -> None:
    service = SearchService(searxng_public_instances_enabled=False)
    provider = EvidenceProvider()
    service._providers = [provider]
    results = service.search_comprehensive_intel('600519', '样本', profile='business')
    assert set(results) == {'business', 'products', 'industry'}
    assert len(provider.queries) == 3
    assert all('最新消息' not in q and '目标价' not in q for q in provider.queries)
    assert all(r.results[0].published_date is None for r in results.values())
    assert all(len(r.results)==1 for r in results.values())


def test_business_evidence_survives_model_context_report_and_reopen(financial_sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from api.v1.endpoints.fundamentals import router
    from src.agent.llm_adapter import LLMResponse, ToolCall
    from src.services import fundamentals_service as service
    from src.storage import get_db
    search = SearchService(searxng_public_instances_enabled=False)
    provider = EvidenceProvider()
    search._providers = [provider]
    monkeypatch.setattr('src.search_service._search_service', search)
    class Model:
        def call_with_tools(self, messages: Any, tools: Any, timeout: float | None = None) -> LLMResponse:
            assert date.today().isoformat() in messages[0]['content']
            assert '目标价' not in messages[0]['content'] or '不要求评级或目标价' in messages[0]['content']
            assert '结论必须带评级' not in messages[0]['content']
            names = {t['function']['name'] for t in tools}
            assert names == {'search_comprehensive_intel'}
            if messages[-1]['role'] != 'tool':
                return LLMResponse(provider='fixture',tool_calls=[ToolCall('call1','search_comprehensive_intel',{'stock_code':'600519','stock_name':'样本'})])
            data = json.loads(messages[-1]['content'])
            assert 'report' not in data
            assert messages[-1]['content'].count('https://example.org/disclosure') == 1
            record = data['dimensions']['business']['results'][0]
            assert record['url']=='https://example.org/disclosure'
            assert record['published_date'] is None
            return LLMResponse(provider='fixture',content=json.dumps({'business_model':{'说明':'主营设备'},'narrative':'资料出处 '+record['url']+'；发布日期未知','data_gaps':['收入结构缺失'],'score':None}))
    monkeypatch.setattr('src.services.deep_research_service._get_dual_track_adapter', Model)
    monkeypatch.setattr(service,'_REPORT_DIR',tmp_path/'reports')
    app = FastAPI()
    app.include_router(router,prefix='/api/v1/fundamentals')
    with TestClient(app) as client:
        out = client.post('/api/v1/fundamentals/generate',params={'stock_code':'600519','stock_name':'样本','force_refresh':True}).json()
        try:
            assert out['dims']['business']['status']=='ok'
            assert 'https://example.org/disclosure' in out['markdown']
            assert '收入结构缺失' in out['markdown']
            assert len(provider.queries)==3
            saved = client.get('/api/v1/fundamentals/reports/'+out['report_id']).json()
            assert saved['data']['markdown']==out['markdown']
        finally:
            if out.get('report_id'):get_db().delete_fundamentals_report(out['report_id'])
