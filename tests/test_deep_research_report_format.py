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
