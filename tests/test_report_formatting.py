"""Report display regressions: real templates and saved-report API responses.
Run: python -m pytest tests/test_report_formatting.py
No provider calls; historical files are isolated in pytest's temporary directory.
"""
import json
from pathlib import Path
from typing import Any, Optional
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
        'payload_version': 2,
        'stock_code': '300260', 'stock_name': '样本',
        'generated_at': '2026-11-05T14:00:00',
        'identity': {'sw_chain': {}, 'subject_status': 'missing',
                     'membership': {'verified': False, 'member_count': None}},
        'background': {
            'policy': {'lean': None, 'label': '未知', 'gaps': []},
            'base_rate': {'base_rate': .5, 'percentile': None, 'table_version': 'v1',
                          'all_rates': [], 'is_default': True},
            'intraday': {'sector_found': False, 'row': {}, 'gaps': ['板块表为空']},
        },
        'concepts': {'items': [], 'source': 'ifind', 'gaps': []},
        'assessment': {'status': 'insufficient_data', 'score': None,
                       'reasons': ['主行业身份未取得：识别链未返回行业数据']},
        'gaps': ['板块识别失败：无行业数据'],
    }
    md = sector._render('样本', '300260', 'fixture', 'fixture', data)
    assert '<font' not in md and '🔴 **基率表版本' in md
    assert '资料不足' in md and '未评分' not in md.split('资料状态说明')[-1]
    assert '板块识别失败：无行业数据' in md


def test_sector_intraday_tie_renders_real_momentum_formula() -> None:
    """并列最高真实渲染（issue #81 第五轮口径收敛）。

    [5, 5, 3]：目标并列最高 → rank 1、momentum = 1/2×100 = 50。正文口径
    必须是真实公式（严格低于目标的有效样本数÷(有效样本数−1)×100，并列
    不计入「低于」数），同一行不得再承诺「最低=0 分、最高=100 分」——
    该承诺与并列场景 momentum=50 直接矛盾（standards P2 反例）。
    """
    data: dict[str, Any] = {
        'payload_version': 2,
        'stock_code': '300260', 'stock_name': '样本',
        'generated_at': '2026-11-05T14:00:00',
        'identity': {'sw_chain': {}, 'subject_status': 'missing',
                     'membership': {'verified': False, 'member_count': None}},
        'background': {
            'policy': {'lean': None, 'label': '未知', 'gaps': []},
            'base_rate': {'base_rate': .5, 'percentile': None, 'table_version': 'v1',
                          'all_rates': [], 'is_default': True},
            'intraday': {
                'sector_found': True,
                'row': {'board_name': '白酒Ⅱ', 'board_code': 'BK1277',
                        'change_pct': 5.0, 'turnover_rate': 2.3, 'amount': 3.1e9,
                        'up_count': 18, 'down_count': 2, 'leader': '样本股',
                        'leader_change_pct': 5.5},
                'momentum': 50.0, 'activity': 100.0, 'amount_activity': 100.0,
                'breadth': 90.0, 'rank_of': 1, 'total_boards': 3, 'gaps': [],
            },
        },
        'concepts': {'items': [], 'source': 'ifind', 'gaps': []},
        'assessment': {'status': 'unscored', 'score': None,
                       'reasons': ['首版不产出综合评分']},
        'gaps': [],
    }
    md = sector._render('样本', '300260', 'fixture', 'fixture', data)
    # 并列事实行：rank 1 / 3、momentum 50
    assert '全表排名：1 / 3' in md
    assert '动量（涨跌幅有效样本分位） | 50.0' in md
    # 口径文字 = 真实公式；并列不计入「低于」数
    assert '严格低于目标的有效样本数' in md and '并列' in md
    # 旧「最低=0 分、最高=100 分」承诺不得再出现（与并列 momentum=50 矛盾）
    assert '最低=0 分' not in md and '最高=100 分' not in md


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


def _sector_v2_data() -> dict[str, Any]:
    """confirmed + 合格趋势/同业的 v2.1 载荷（issue #82 渲染基线）。"""
    trend_dates = [f'2026-10-{d:02d}' for d in range(9, 21)] + \
                  [f'2026-11-{d:02d}' for d in range(2, 5)]
    trend_obs = [{'date': d, 'close': 1000.0 + i} for i, d in enumerate(trend_dates)]
    bench_obs = [{'date': d, 'close': 4000.0 + i} for i, d in enumerate(trend_dates)]
    return {
        'payload_version': 2,
        'stock_code': '600519', 'stock_name': '贵州茅台',
        'generated_at': '2026-11-05T14:00:00',
        'identity': {
            'sw_chain': {'l1': '食品饮料', 'l2': '白酒Ⅱ', 'l3': '白酒Ⅲ',
                         'l3_code': '851251.SI', 'gaps': []},
            'subject_status': 'confirmed', 'industry_em': '白酒Ⅱ',
            'board_name': '白酒Ⅱ', 'board_code': 'BK1277',
            'membership': {'verified': True, 'member_count': 20},
            'observed_at': '2026-11-05T14:00:00', 'vendor_as_of': None,
            'classification_system': '东财行业板块', 'classification_level': None,
            'classification_version': None, 'sector_name': '白酒Ⅱ', 'gaps': [],
        },
        'background': {
            'policy': {'lean': 'neutral', 'label': '中性', 'gaps': []},
            'base_rate': {'base_rate': .55, 'percentile': .9, 'table_version': 'v1',
                          'table_updated': '2026-09-29', 'table_industry_count': 21,
                          'hit_keyword': '白酒', 'all_rates': [], 'is_default': False},
            'intraday': {
                'sector_found': True,
                'row': {'board_name': '白酒Ⅱ', 'board_code': 'BK1277',
                        'change_pct': 1.65, 'turnover_rate': 2.3, 'amount': 3.1e9,
                        'up_count': 18, 'down_count': 2, 'leader': '样本股',
                        'leader_change_pct': 5.5},
                'momentum': 50.0, 'activity': 100.0, 'amount_activity': 100.0,
                'breadth': 90.0, 'rank_of': 1, 'total_boards': 3, 'gaps': [],
            },
        },
        'concepts': {'items': [], 'source': 'ifind', 'gaps': []},
        'sector_trend': {
            'eligible': True, 'window_completed_sessions': 20, 'interval_count': 19,
            'start_date': '2026-10-09', 'end_date': '2026-11-04',
            'expected_dates': trend_dates,
            'qualified_sessions': 20, 'observations': trend_obs,
            'period_return_pct': 5.1234,
            'benchmark': {'name': '沪深300', 'symbol': 'sh000300',
                          'source': '东财指数日线', 'start_date': '2026-10-09',
                          'end_date': '2026-11-04', 'qualified_sessions': 20,
                          'observations': bench_obs,
                          'period_return_pct': 3.8889, 'gaps': []},
            'relative_return_pp': 1.2345,
            'source': '东财行业板块日线', 'observed_at': '2026-11-05T14:00:02',
            'gaps': [],
        },
        'sector_peers': {
            'eligible': True,
            'selection_rule': '同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）',
            'amount_unit': '元',
            'amount_provenance': '成交额单位为元：按同字段同接口参数推断（板块接口文档未标注单位）',
            'items': [
                {'code': '000858', 'name': '五粮液', 'amount': 2.2e9,
                 'change_pct': 2.0, 'is_target': False},
                {'code': '600519', 'name': '贵州茅台', 'amount': 3.1e9,
                 'change_pct': 1.65, 'is_target': True},
            ],
            'target_in_amount_top5': True, 'target_change_pct_rank': 2,
            'sample_size': 2, 'snapshot_observed_at': '2026-11-05T14:00:01',
            'snapshot_rows': 20, 'rankable_rows': 20,
            'source': '东财行业板块成分（身份验证同一快照）', 'gaps': [],
        },
        'assessment': {'status': 'unscored', 'completeness': 'complete',
                       'score': None, 'reasons': []},
        'gaps': [],
    }


def test_sector_trend_and_peers_render_with_exact_units() -> None:
    """趋势/同业节：单位与口径准确（百分点、19 间隔、非龙头、不同时间口径）。

    issue #82 rework：实际 20 日日线以表格进正文（板块/基准独立），成交额带单位。
    """
    md = sector._render('贵州茅台', '600519', 'fixture', 'fixture', _sector_v2_data())
    # 趋势：窗口起止 + 19 间隔复合 + 相对差单位为百分点
    assert '2026-10-09 → 2026-11-04' in md
    assert '19 个交易日间隔复合' in md and '20日收益' not in md
    assert '区间收益差 1.2345 个百分点' in md
    assert '板块区间收益：**5.1234%' in md
    assert '板块合格观测：20/20' in md
    # 实际日线观测表（板块 + 基准独立渲染，覆盖 20 天）
    assert '| 日期 | 板块收盘（板块指数点位） |' in md
    assert '| 日期 | 沪深 300 收盘（指数点位） |' in md  # 渲染后处理含盘古空格
    assert '| 2026-10-09 | 1000.0 |' in md
    assert '| 2026-10-09 | 4000.0 |' in md
    # 同业：选取规则 + 单位 + provenance + 目标位置 + 非市值/基本面龙头声明
    assert '成交活跃代表，非市值/基本面龙头' in md
    assert '| 代码 | 名称 | 当日成交额（元） | 当日涨跌幅 | 目标股 |' in md
    assert '成交额单位：元' in md
    assert '按同字段同接口参数推断' in md
    assert '排第 **2** / 2' in md
    assert '不同时间口径' in md
    # 章节顺序（#79 叙事：定位→当日→趋势→同业→政策→基率→概念）
    order = [md.index(s) for s in ('## 一、', '## 二、', '## 三、', '## 四、',
                                   '## 五、', '## 六、', '## 七、')]
    assert order == sorted(order)


def test_sector_trend_unconfirmed_renders_not_executed() -> None:
    """身份未确认：趋势/同业如实「未执行」，不渲染任何趋势数值。"""
    data = _sector_v2_data()
    data['identity']['subject_status'] = 'unverified'
    data['identity']['membership'] = {'verified': False, 'member_count': 20}
    data['sector_trend'] = {
        'eligible': False, 'window_completed_sessions': 20, 'interval_count': 19,
        'start_date': None, 'end_date': None, 'expected_dates': [],
        'period_return_pct': None, 'benchmark': None, 'relative_return_pp': None,
        'source': None, 'observed_at': '2026-11-05T14:00:02',
        'gaps': ['板块趋势：主行业身份未确认（confirmed 前置不满足），不执行板块趋势查询'],
    }
    data['sector_peers'] = {
        'eligible': False,
        'selection_rule': '同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）',
        'items': [], 'target_in_amount_top5': None, 'target_change_pct_rank': None,
        'sample_size': 0, 'snapshot_observed_at': None, 'source': None,
        'gaps': ['同业比较：主行业身份未确认（confirmed 前置不满足），不执行同业选取'],
    }
    data['assessment']['completeness'] = 'partial'
    md = sector._render('贵州茅台', '600519', 'fixture', 'fixture', data)
    assert '板块趋势未执行' in md and '同业比较未执行' in md
    assert '5.1234' not in md  # 无资格时不给任何趋势数值


def test_sector_trend_gap_keeps_partial_and_reasons() -> None:
    """趋势未取得：块内缺口逐条渲染，不伪 complete。"""
    data = _sector_v2_data()
    data['sector_trend'] = {
        'eligible': True, 'window_completed_sessions': 20, 'interval_count': 19,
        'start_date': '2026-10-09', 'end_date': '2026-11-04',
        'expected_dates': ['2026-10-09', '2026-10-12'],
        'period_return_pct': None, 'benchmark': None, 'relative_return_pp': None,
        'source': None, 'observed_at': '2026-11-05T14:00:02',
        'gaps': ['板块日线（白酒Ⅱ BK1277）：期望 20 个已完成交易日中取得 19 天合格收盘，'
                 '缺失或收盘无效 1 天（2026-10-09），不取交集/旧日凑数，区间收益不计算'],
    }
    data['assessment']['completeness'] = 'partial'
    data['assessment']['reasons'] = ['板块近期趋势未取得：板块日线（白酒Ⅱ BK1277）：期望 20 个'
                                     '已完成交易日中取得 19 天合格收盘，缺失或收盘无效 1 天'
                                     '（2026-10-09），不取交集/旧日凑数，区间收益不计算']
    md = sector._render('贵州茅台', '600519', 'fixture', 'fixture', data)
    assert '板块趋势未产出' in md
    assert '不取交集/旧日凑数' in md
    assert '部分' in md  # completeness partial 首屏可见


def test_benchmark_partial_observations_render_independent_of_return() -> None:
    """基准部分观测（收益 None）：实际观测与覆盖独立进正文（不再嵌收益分支）。"""
    data = _sector_v2_data()
    bench_obs = data['sector_trend']['benchmark']['observations'][:14]
    data['sector_trend']['benchmark'] = {
        **data['sector_trend']['benchmark'],
        'qualified_sessions': 14, 'observations': bench_obs,
        'period_return_pct': None,
        'gaps': ['基准日线（沪深300）：期望 20 个已完成交易日中取得 14 天合格收盘，'
                 '缺失 6 天（2026-11-04 等），不取交集/旧日凑数，区间收益不计算'],
    }
    data['sector_trend']['relative_return_pp'] = None
    md = sector._render('贵州茅台', '600519', 'fixture', 'fixture', data)
    assert '同窗口合格观测 14/20 个交易日' in md
    assert '| 日期 | 沪深 300 收盘（指数点位） |' in md
    assert '| 2026-10-09 | 4000.0 |' in md    # 实际观测渲染（首条）
    assert '| 2026-11-03 | 4013.0 |' in md    # 实际观测渲染（末条）
    assert '| 2026-11-04 | 4014.0 |' not in md  # 缺失日不占位（板块表另有该日期行）
    assert '同窗口区间收益' not in md        # 收益仅 20/20 才给
    assert '本窗口区间收益不合格或未取得' in md
    assert '相对表现不计算' in md  # 基准侧不合格说明（板块侧收益独立保留）


def test_target_only_peers_sample_renders_no_fake_rank() -> None:
    """目标-only 同业样本（无成交额数据）：目标行带实际字段渲染，
    不产 1/1 伪名次，比较范围缺口可见。"""
    data = _sector_v2_data()
    data['sector_peers'] = {
        **data['sector_peers'],
        'items': [{'code': '600519', 'name': '贵州茅台', 'amount': None,
                   'change_pct': 1.65, 'is_target': True}],
        'target_in_amount_top5': None, 'target_change_pct_rank': None,
        'sample_size': 1, 'rankable_rows': 0,
        'gaps': ['同业比较：成分快照无成交额列，成交额均无数据（缺列不逐行伪计），'
                 '无法按成交活跃选取代表，不输出成交额前五（成员与实际字段保留）',
                 '同业比较：无成交额可排名同行，样本仅保留目标股 600519'
                 '（无同业可比代表，相对位置不输出，不产出 1/1 伪名次）'],
    }
    md = sector._render('贵州茅台', '600519', 'fixture', 'fixture', data)
    assert '| 600519 | 贵州茅台 | N/A | 1.65% | ✔ |' in md
    assert '不按成交额排序' in md  # 无数据不冒称按成交额降序
    assert '排第 **1** / 1' not in md
    assert '无成交额列' in md
    assert '无可比' in md


# ---- issue #83 live 表格标题分隔符：GFM 结构断言（真实渲染器） ----

_LIVE_PIPE_TITLE = '央行重申适度宽松政策，白酒行业持续去库存| 财经日日评'
_LIVE_PIPE_URL = 'https://www.163.com/dy/article/LB1OOAJ005199A0B.html'


def _gfm_table_rows(md_src: str) -> tuple[list[list[str]], list[tuple[str, str]]]:
    """markdown-it-py 真实 GFM 解析（litellm→rich 硬依赖链，仓库环境必在）。

    返回 (每行单元格纯文本, 全部链接 (href, 链接文本))——用真实 GFM 渲染器
    而非字符串子串断言，列错位/丢列在解析层暴露。
    """
    from markdown_it import MarkdownIt

    tokens = MarkdownIt('commonmark').enable('table').parse(md_src)
    rows: list[list[str]] = []
    cur: list[str] = []
    links: list[tuple[str, str]] = []
    cell_chars: list[str] = []
    href: Optional[str] = None
    in_cell = False
    for t in tokens:
        if t.type == 'tr_open':
            cur = []
        elif t.type in ('th_open', 'td_open'):
            cell_chars, in_cell = [], True
        elif t.type == 'inline' and in_cell:
            for ch in t.children or []:
                if ch.type == 'link_open':
                    raw_href = dict(ch.attrs).get('href')
                    href = raw_href if isinstance(raw_href, str) else None
                elif ch.type == 'text':
                    cell_chars.append(ch.content)
                    if href is not None:
                        links.append((href, ch.content))
                        href = None
            cur.append(''.join(cell_chars))
        elif t.type == 'tr_close':
            rows.append(cur)
            in_cell = False
    return rows, links


def _catalyst_live_payload() -> dict[str, Any]:
    """真实 sa_202611061255 形状：provider/article 两条 date_basis + 背景。"""
    return {
        'eligible': True, 'query': '白酒 行业 政策 新闻', 'window_days': 3,
        'window_start': '2026-11-04', 'window_end': '2026-11-06',
        'observed_at': '2026-11-06T12:55:13', 'provider': 'Zhipu',
        'items': [
            {'evidence_id': 'cat-1', 'title': '白酒去库存近尾声！连续五个旺季动销下滑后',
             'url': 'https://feng.ifeng.com/c/8xlsH9KxyTm',
             'source_domain': 'feng.ifeng.com', 'snippet': None,
             'published_date': '2026-11-04', 'date_basis': 'provider_published',
             'date_basis_detail': None, 'same_event_urls': [],
             'final_url': 'https://feng.ifeng.com/c/8xlsH9KxyTm'},
            {'evidence_id': 'cat-2', 'title': _LIVE_PIPE_TITLE,
             'url': _LIVE_PIPE_URL, 'source_domain': 'www.163.com',
             'snippet': None, 'published_date': '2026-11-06',
             'date_basis': 'article_metadata',
             'date_basis_detail': (
                 '<meta property="article:published_time" '
                 'content="2026-11-06T08:33:03+08:00">'),
             'same_event_urls': [], 'final_url': _LIVE_PIPE_URL},
        ],
        'background_items': [
            {'evidence_id': 'bg-2',
             'title': '白酒最弱三季报落地，五年调整迎来扭转契机？三重积极信号',
             'url': 'https://finance.eastmoney.com/a/202611023918912943.html',
             'source_domain': 'finance.eastmoney.com', 'snippet': None,
             'published_date': None,
             'downgrade_reason': '文章无可用发布日期证据',
             'final_url': 'https://finance.eastmoney.com/a/202611023918912943.html'},
        ],
        'dropped_duplicate_url': 0, 'dropped_syndicated': 0,
        'dropped_offwindow': 0, 'dropped_offtopic': 1,
        'dropped_invalid_source': 0, 'dropped_invalid_field': 0,
        'source': 'Zhipu', 'gaps': [],
    }


def test_sector_catalyst_pipe_title_keeps_gfm_columns() -> None:
    """真实 live 标题含字面 ``|``：GFM 表格 5 列结构、列语义、链接完整性。

    缺陷实录（sa_202611061255 cat-2）：未转义时 ``|`` 成列定界符，日期列
    被标题后缀+URL 顶替、date_basis 列被日期顶替、来源列被 date_basis
    顶替、真实来源域名被丢弃。qualified 与 background 表同一渲染缝。
    """
    data = _sector_v2_data()
    data['sector_catalysts'] = _catalyst_live_payload()
    md = sector._render('贵州茅台', '600519', 'fixture', 'fixture', data)
    rows, links = _gfm_table_rows(md)
    header_idx = rows.index(['证据 ID', '标题（链接为原始引用）', '发布日期',
                             '日期口径', '来源'])
    cat_rows = rows[header_idx + 1:header_idx + 3]
    # 结构：两行数据各 5 列（不是未转义时的错位 5 列截断——列语义逐列核对）
    assert cat_rows[0] == ['cat-1', '白酒去库存近尾声！连续五个旺季动销下滑后',
                           '2026-11-04', '供应商发布字段', 'feng.ifeng.com']
    assert cat_rows[1] == ['cat-2', _LIVE_PIPE_TITLE, '2026-11-06',
                           '文章元数据（含原始依据）', 'www.163.com']
    # 链接完整性：原始 href + 标题字面文本（含 ``|``）可见、不在链接外散落
    assert (_LIVE_PIPE_URL, _LIVE_PIPE_TITLE) in links
    # 背景表：同缝修复，5 列、限制说明在末列（未左移）
    bg_idx = rows.index(['证据 ID', '标题（链接为原始引用）', '发布日期',
                         '来源', '限制说明'])
    assert rows[bg_idx + 1] == [
        'bg-2', '白酒最弱三季报落地，五年调整迎来扭转契机？三重积极信号',
        '未知', 'finance.eastmoney.com', '文章无可用发布日期证据']
    # 原始 JSON 载荷不被渲染转义污染（渲染层投影，数据层原样）
    assert data['sector_catalysts']['items'][1]['title'] == _LIVE_PIPE_TITLE


def test_md_link_text_ordinary_punctuation_self_consistent() -> None:
    """转义方案对普通反斜杠/竖线/换行自洽，不引入相邻投影缺陷。"""
    rows, _ = _gfm_table_rows(
        '| A | B |\n|---|---|\n| x | ['
        + sector._md_link_text('去库存\\|复盘 | 结论') + '](https://e.com/a) |\n'
    )
    # 原文 ``\`` + 插入的 ``\|`` 不重组出新定界符：数据行仍 2 列、文本字面
    assert rows[1:] == [['x', '去库存\\|复盘 | 结论']]
    rows, _ = _gfm_table_rows(
        '| A | B |\n|---|---|\n| x | ['
        + sector._md_link_text('标题一\n标题二') + '](https://e.com/b) |\n'
    )
    # 原始换行折叠为空格：表格行不被拆断
    assert rows[1:] == [['x', '标题一 标题二']]


def test_md_link_text_bracket_backslash_keeps_whole_anchor() -> None:
    """反斜杠加倍不 literalize 括号时改变转义奇偶的相邻回归（root 反例）。

    - ``政策\\]解读``：``\\`` 若只加倍，渲染出字面 ``\\`` 后紧随的 ``]``
      提前终止链接文本 → adopted URL anchor 由 1 变 0（整段变普通文本）；
    - ``政策\\[解读| 资讯``：同理 ``\\[`` 奇偶改变后标题前缀脱落链接外。
    filter 现同时 literalize 链接文本括号定界符：完整标题在单一原始 href
    anchor 内、表格 5 列不破。
    """
    titles = ['政策\\]解读', '政策\\[解读| 资讯']
    for title in titles:
        rows, links = _gfm_table_rows(
            '| A | B |\n|---|---|\n| x | ['
            + sector._md_link_text(title) + '](https://e.com/adopt) |\n'
        )
        assert rows[1:] == [['x', title]]  # 2 列、字面完整标题
        assert links == [('https://e.com/adopt', title)]  # whole anchor
        # 平衡括号标题同样整体在链接内（显示字面不变）
    rows, links = _gfm_table_rows(
        '| A | B |\n|---|---|\n| x | ['
        + sector._md_link_text('[专题]白酒| 周报') + '](https://e.com/bal) |\n'
    )
    assert rows[1:] == [['x', '[专题]白酒| 周报']]
    assert links == [('https://e.com/bal', '[专题]白酒| 周报')]
