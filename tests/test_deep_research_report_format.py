"""Replay structured research through parsers and both report templates.
Run: python -m pytest tests/test_deep_research_report_format.py
No live model or provider calls; records use the observed production payload shape.
"""
import pytest
from src.deep_research.researchers.ownership import parse
from src.deep_research_dims.render import build_view, render_markdown
from tests.test_deep_research_dims import _all_default_dims


@pytest.mark.parametrize('template', ['deep_research_report_v2.j2', 'deep_research_dual_track.j2'])
def test_ownership_objects_and_disclosure_sections_render_as_markdown(template):
    dims = _all_default_dims()
    dims['ownership'], _ = parse({'top_holders': [{'rank_or_note': '股东名单缺失', 'detail': '未取得原始公告', 'source': '未验证'}], 'executives': [{'name': '示例高管', 'source': 'announcement', 'detail': '公开职务'}]}, 0)
    markdown = render_markdown(build_view('样本', '300260', 'fixture', [], dims, []), template_name=template)
    assert '<details>' not in markdown and '<summary>' not in markdown
    assert 'source=None' not in markdown and "name='" not in markdown
    if template == 'deep_research_report_v2.j2':
        assert '示例高管' in markdown and '公开职务' in markdown and 'announcement' in markdown
        assert '股东名单缺失' in markdown and '未取得原始公告' in markdown


def test_legacy_markdown_is_normalized_without_changing_code():
    from src.deep_research_dims.render import normalize_saved_markdown
    raw = '<details>\n<summary>术语</summary>\n\n- name="{\'rank_or_note\': \'名单缺失\', \'detail\': \'未取得公告\', \'source\': \'未验证\'}" source=None\n\n</details>\n\n```html\n<details><summary>示例</summary></details>\n```\n'
    rendered = normalize_saved_markdown(raw)
    assert '- 名单缺失：未取得公告（来源：未验证）' in rendered
    assert 'source=None' not in rendered
    assert rendered.count('<details>') == 1  # code example only
    assert '```html\n<details><summary>示例</summary></details>\n```' in rendered
    assert normalize_saved_markdown(rendered) == rendered


def test_saved_report_detail_and_download_agree_without_rewriting_file(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints import deep_research as endpoint
    from src.services import deep_research_service as service
    report_id = '300260_202610130930'
    path = tmp_path / (report_id + '.md')
    raw = '<!-- dim:ownership -->\n<details>\n<summary>历史报告</summary>\n\n- name=\'示例高管\' source=\'announcement\'\n\n</details>\n'
    path.write_text(raw)
    metadata = {'id': report_id, 'md_path': str(path), 'stock_code': '300260', 'stock_name': '样本'}
    record = SimpleNamespace(md_path=str(path), to_dict=lambda: metadata.copy())
    monkeypatch.setattr(service, 'get_db', lambda: SimpleNamespace(get_deep_research_report=lambda _: record))
    monkeypatch.setattr(endpoint, 'get_deep_research_dir', lambda: tmp_path)
    app = FastAPI()
    app.include_router(endpoint.router, prefix='/api/v1/deep-research')
    with TestClient(app) as client:
        detail = client.get(f'/api/v1/deep-research/reports/{report_id}')
        download = client.get(f'/api/v1/deep-research/reports/{report_id}/markdown')
    assert detail.status_code == download.status_code == 200
    assert detail.json()['data']['markdown'] == download.text
    assert '<details>' not in download.text and 'name=' not in download.text
    assert '<!-- dim:' not in download.text
    assert '来源：announcement' in download.text
    assert 'filename*=' in download.headers['content-disposition']
    assert path.read_text() == raw


def test_partial_report_keeps_useful_content_before_limits_and_reference_in_its_subreport():
    from src.schemas.deep_research_dims import RetainedEvidence
    from src.deep_research_dims.render import split_dim_sections
    dims = _all_default_dims()
    dims['business'] = dims['business'].model_copy(update={
        'status': 'degraded', 'degraded_reason': '模型请求超时，未完成本维度研究',
        'narrative': '已取得主营产品收入。',
        'retained_evidence': [RetainedEvidence(tool='search', title='公司公告', url='https://example.com/report', source='公告', snippet='主营收入资料')],
    })
    dims['ownership'] = dims['ownership'].model_copy(update={'status': 'skipped'})
    md = render_markdown(build_view('样本', '300260', 'fixture', [], dims, []))
    assert '降级占位' not in md and '省钱模式' not in md
    assert '正常17项' in md and '未完整完成1项' in md and '主动未选1项' in md
    business = split_dim_sections(md)['business']
    assert business.index('已取得主营产品收入') < business.index('模型请求超时')
    assert '[公司公告](https://example.com/report)' in business
    assert '尚未完整完成' in business
    assert '独立技术点位' in md and 'EV：**N/A' in md


def test_known_historical_scope_label_changes_without_inventing_failure_cause():
    from src.deep_research_dims.render import normalize_saved_markdown
    raw = '省钱模式：自选维度\n⚠️ 本维度生成不充分（未知旧错误），以下内容为降级占位。\n```\n省钱模式\n```\n'
    md = normalize_saved_markdown(raw)
    assert md.startswith('分析范围：自选维度')
    assert '未知旧错误' in md and '模型请求超时' not in md
    assert '降级占位' not in md and '```\n省钱模式\n```' in md


def test_shared_financial_template_explains_unknown_period_without_changing_eligibility():
    from src.services.fundamentals_service import _render_markdown
    from tests.test_f1_financial_quality import context
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim
    dim = build_fundamental_dim(context({'roe': 4.56789})).model_dump(mode='json')
    field = dim['data_quality']['fields']['roe']
    field['quality'] = {'status': 'not_comparable', 'reason_codes': ['period_unknown', 'adjustment_mismatch', 'adjustment_unknown']}
    before = field.copy()
    md = _render_markdown('样本', '600519', 'fixture', {'financial': dim})
    assert '财期未知' in md and '调整口径不同' in md and '尚不能直接对比' in md
    assert '4.57%' in md and '百分点' in md
    assert 'period_unknown' not in md and 'adjustment_unknown' not in md and '调整口径未知' in md
    assert field == before


def test_model_timeout_with_acquired_facts_does_not_claim_source_is_unconnected():
    from src.schemas.deep_research_dims import RetainedFact
    from src.schemas.research_framework import DimensionScore, IndicatorScore, ResearchFramework
    from src.scoring.indicators_v2 import aggregate_v2_dimensions, framework_total_v2
    from src.scoring.weights_v2 import DIMENSION_WEIGHTS_V2
    from src.deep_research_dims.render import split_dim_sections

    dims = _all_default_dims()
    scores = aggregate_v2_dimensions({})
    dims['six_dim'] = dims['six_dim'].model_copy(update={'framework': ResearchFramework(
        dimension_total=framework_total_v2(scores),
        dimensions=[DimensionScore(
            dimension=row['dimension'], weight=DIMENSION_WEIGHTS_V2[row['dimension']],
            score=row['score'], indicators=[IndicatorScore(**item) for item in row['indicators']],
        ) for row in scores],
    )})
    fact = RetainedFact(tool='get_capital_flow', label='5交易日主力净流入', value='124384302.0',
                        source='choice', period='2026-10-15', unit='CNY / CNY', qualification='unverified')
    dims['capital'] = dims['capital'].model_copy(update={
        'status': 'degraded', 'degraded_reason': '模型请求超时，未完成本维度研究',
        'retained_facts': [fact],
    })
    before = dims['six_dim'].model_dump()
    md = render_markdown(build_view('样本', '300260', 'fixture', [], dims, []))
    assert '尚未接线' not in md
    assert '当前未形成可用于该项评分的输入' in md
    capital = split_dim_sections(md)['capital']
    assert '模型请求超时' in capital and '124384302.0' in capital
    assert '人民币 / 人民币' not in capital
    assert '人民币' in capital and 'choice' in capital and '2026-10-15' in capital
    assert dims['capital'].retained_facts[0] == fact and fact.unit == 'CNY / CNY'
    assert dims['six_dim'].model_dump() == before
