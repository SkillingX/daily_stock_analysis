// 报告速览首屏组件行为测试（issue #103）：一级排名与行情日期标注、二级目录
// 映射（confirmed/missing）、5/20/60 窗口切换（存档数据本地切换，不发请求）、
// 序列缺口留空、催化三态、旧报告（无新键）不渲染。
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

// recharts 依赖布局尺寸，jsdom 下以行为等价替身渲染（交互逻辑不依赖图表库）
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="chart-container">{children}</div>
  ),
  LineChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="line-chart">{children}</div>
  ),
  Line: ({ dataKey, name }: { dataKey: string; name?: string }) => (
    <div data-testid={`chart-line-${dataKey}`} data-name={name} />
  ),
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Legend: () => null,
  CartesianGrid: () => null,
  ReferenceLine: () => null,
}));

import SectorFirstScreen from '../SectorFirstScreen';

function confirmedAnalysis() {
  return {
    identity: {
      subject_status: 'confirmed',
      board_name: '非银金融',
      board_code: '801790.SL',
    },
    background: {
      intraday: {
        sector_found: true,
        snapshot_date: '2026-11-19',
        snapshot_mode: 'target',
        rank_of: 5,
        total_boards: 31,
        row: { board_name: '非银金融', change_pct: -0.5 },
      },
    },
    sector_l2_identity: {
      status: 'confirmed',
      l2_name: '保险Ⅱ',
      l2_index_code: '801194.SL',
      l1_parent_name: '非银金融',
      directory_version: 'sw2021-l2-20261119',
      observation_date: '2026-11-19',
      gaps: [],
    },
    sector_trend_windows: [
      {
        window_days: 5,
        point_count: 6,
        start_date: '2026-11-13',
        end_date: '2026-11-19',
        series: [
          {
            role: 'l2_industry',
            name: '保险Ⅱ',
            index_code: '801194.SL',
            expected_sessions: 6,
            qualified_sessions: 6,
            observations: [
              { date: '2026-11-13', close: 1000 },
              { date: '2026-11-19', close: 1010 },
            ],
            period_return_pct: 1.0,
            gaps: [],
          },
          {
            role: 'l1_industry',
            name: '非银金融',
            index_code: '801790.SL',
            expected_sessions: 6,
            qualified_sessions: 6,
            observations: [
              { date: '2026-11-13', close: 2000 },
              { date: '2026-11-19', close: 1990 },
            ],
            period_return_pct: -0.5,
            gaps: [],
          },
          {
            role: 'benchmark',
            name: '沪深300',
            index_code: '000300.SH',
            expected_sessions: 6,
            qualified_sessions: 6,
            observations: [
              { date: '2026-11-13', close: 4000 },
              { date: '2026-11-19', close: 3900 },
            ],
            period_return_pct: -2.5,
            gaps: [],
          },
        ],
        gaps: [],
      },
      {
        window_days: 20,
        point_count: 21,
        start_date: '2026-10-23',
        end_date: '2026-11-19',
        series: [],
        gaps: ['受控：20 日窗口未取得'],
      },
      {
        window_days: 60,
        point_count: 61,
        start_date: '2026-08-19',
        end_date: '2026-11-19',
        series: [
          {
            role: 'l2_industry',
            name: '保险Ⅱ',
            index_code: '801194.SL',
            expected_sessions: 61,
            qualified_sessions: 60,
            observations: [],
            period_return_pct: null,
            gaps: ['60 日窗口缺合格收盘点（2026-08-19），该序列本窗口收益与曲线留空（不取旧日凑数）'],
          },
          {
            role: 'benchmark',
            name: '沪深300',
            index_code: '000300.SH',
            expected_sessions: 61,
            qualified_sessions: 61,
            observations: [
              { date: '2026-08-19', close: 4500 },
              { date: '2026-11-19', close: 3900 },
            ],
            period_return_pct: -13.16,
            gaps: [],
          },
        ],
        gaps: [],
      },
    ],
    sector_catalysts: {
      eligible: true,
      items: [
        {
          evidence_id: 'cat-1',
          title: '保险行业监管新规发布',
          url: 'https://example.com/news/1',
          source_domain: 'example.com',
          published_date: '2026-11-18',
        },
      ],
      gaps: [],
    },
  } as Record<string, unknown>;
}

describe('SectorFirstScreen', () => {
  it('renders L1 rank, quote date and L2 mapping when confirmed', () => {
    render(<SectorFirstScreen analysis={confirmedAnalysis()} />);
    expect(screen.getByText('非银金融（801790.SL）')).toBeTruthy();
    expect(screen.getByText(/5 \/ 31/)).toBeTruthy();
    expect(screen.getByText(/行情日期 2026-11-19/)).toBeTruthy();
    expect(screen.getByText(/保险Ⅱ（801194.SL）/)).toBeTruthy();
    expect(screen.getByText(/父行业：非银金融/)).toBeTruthy();
  });

  it('switches 5/20/60 windows locally without refetching', () => {
    const analysis = confirmedAnalysis();
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    render(<SectorFirstScreen analysis={analysis} />);
    // 默认 20 日窗口（该窗口受控为缺口）
    expect(screen.getByText(/受控：20 日窗口未取得/)).toBeTruthy();
    expect(screen.getAllByText(/数据来源：/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('tab', { name: '5日' }));
    expect(screen.getByText(/2026-11-13/)).toBeTruthy();
    expect(screen.getByText('+1.00%')).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: '60日' }));
    expect(screen.getByText('-13.16%')).toBeTruthy();
    // 序列级缺口：长窗缺点的序列收益留空并显示缺口
    expect(screen.getAllByText(/留空/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/2026-08-19/).length).toBeGreaterThan(0);
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it('renders L2 mapping gap when missing', () => {
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_l2_identity = {
      status: 'missing',
      l2_name: null,
      l2_index_code: null,
      l1_parent_name: null,
      directory_version: 'sw2021-l2-20261119',
      observation_date: '2026-11-19',
      gaps: ['申万二级归属未取得（参考链未查询/失败或取数预算已耗尽），二级对照未采纳'],
    };
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByText(/二级对照未采纳/)).toBeTruthy();
    expect(screen.queryByText(/801194.SL/)).toBeNull();
  });

  it('distinguishes catalyst outcomes via structured summary', () => {
    const cases: Array<[string, RegExp]> = [
      ['none_found', /未发现合格催化/],
      ['search_failed', /全部检索失败/],
      ['partial_failed', /部分检索渠道失败/],
      ['search_unavailable', /搜索入口未配置/],
      ['budget_exhausted', /取数预算不足/],
    ];
    for (const [state, expected] of cases) {
      const analysis = confirmedAnalysis();
      (analysis as Record<string, unknown>).sector_catalysts = {
        eligible: true,
        items: [],
        first_screen: { state, headline_evidence_id: null },
        // worker/provider 诊断排在 gaps 前面：结构化字段不受其顺序干扰
        gaps: ['行业催化：搜索供应商 X 返回失败（受控诊断）'],
      };
      const { unmount } = render(<SectorFirstScreen analysis={analysis} />);
      expect(screen.getByText(expected)).toBeTruthy();
      unmount();
    }
  });

  it('marks partial search failure beside a found catalyst', () => {
    // issue #105 修复：已找到催化时并列标注部分检索失败（原文与日期照常）
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_catalysts = {
      eligible: true,
      items: [
        {
          evidence_id: 'cat-1',
          title: '保险行业监管新规发布',
          url: 'https://example.com/news/1',
          source_domain: 'example.com',
          published_date: '2026-11-18',
        },
      ],
      first_screen: {
        state: 'found',
        headline_evidence_id: 'cat-1',
        partial_search_failures: 1,
      },
      gaps: [],
    };
    const { unmount } = render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByText('保险行业监管新规发布')).toBeTruthy();
    expect(screen.getByText(/2026-11-18 发布/)).toBeTruthy();
    expect(screen.getByText(/部分检索渠道失败 1 个/)).toBeTruthy();
    unmount();
    // 旧存档（无该键）不标注
    const legacy = confirmedAnalysis();
    (legacy as Record<string, unknown>).sector_catalysts = {
      eligible: true,
      items: [
        {
          evidence_id: 'cat-1',
          title: '保险行业监管新规发布',
          url: 'https://example.com/news/1',
          source_domain: 'example.com',
          published_date: '2026-11-18',
        },
      ],
      gaps: [],
    };
    const { unmount: unmountLegacy } = render(
      <SectorFirstScreen analysis={legacy} />);
    expect(screen.getByText('保险行业监管新规发布')).toBeTruthy();
    expect(screen.queryByText(/部分检索渠道失败/)).toBeNull();
    unmountLegacy();
  });

  it('renders first-screen title with theme foreground token', () => {
    // issue #105 修复：标题与二级身份文字用主题前景色（深色主题可读）
    render(<SectorFirstScreen analysis={confirmedAnalysis()} />);
    const heading = screen.getByRole('heading', { name: '报告速览' });
    expect(heading.className).toContain('text-foreground');
    expect(heading.className).not.toContain('text-primary-text');
    const l2Span = screen.getByText(/保险Ⅱ（801194.SL）/);
    expect(l2Span.className).toContain('text-foreground');
  });

  it('renders nothing for legacy reports without new keys', () => {
    const { container } = render(<SectorFirstScreen analysis={{ payload_version: 3 }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('marks reference-mode rank and quote with reference wording', () => {
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).background = {
      intraday: {
        sector_found: true,
        snapshot_date: '2026-11-18',
        snapshot_mode: 'reference',
        rank_of: 9,
        total_boards: 31,
        row: { board_name: '非银金融', change_pct: 1.2 },
      },
    };
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByText(/参考日排名 9 \/ 31/)).toBeTruthy();
    expect(screen.getByText(/参考日涨跌幅/)).toBeTruthy();
    expect(screen.getByText(/参考口径 2026-11-18/)).toBeTruthy();
    expect(screen.queryByText(/当日排名/)).toBeNull();
  });

  it('surfaces whole-window failure when no windows returned', () => {
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_trend_windows = [];
    (analysis as Record<string, unknown>).sector_trend_windows_gaps = [
      '多窗口趋势：iFinD 指数日线查询失败（受控失败），5/20/60 日窗口未取得'];
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByText(/多窗口趋势未取得/)).toBeTruthy();
    expect(screen.getByText(/受控失败/)).toBeTruthy();
  });

  it('surfaces block gap once when windows exist with a missing series', () => {
    // issue #103 复审：有窗口+缺一条序列时块级缺口仍随趋势块展示一次，
    // 不逐窗口重复（对比全无窗口的互斥分支）
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_trend_windows_gaps = [
      '多窗口趋势：iFinD 日线响应缺 保险Ⅱ 801194.SL 数据行，该序列各窗口未取得'];
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getAllByText(/多窗口趋势缺口/).length).toBe(1);
    expect(screen.getAllByText(/该序列各窗口未取得/).length).toBe(1);
    // 窗口内容照常展示
    expect(screen.getByText(/5 \/ 31/)).toBeTruthy();
  });

  it('hides curve when a fully aligned window return is unpublishable', () => {
    // issue #103 复审二轮：完整窗口但收益不可安全表示（period_return 空 +
    // 序列缺口）→ 收益与曲线一并留空，合格观测保留在表格
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_trend_windows = [
      {
        window_days: 60,
        point_count: 61,
        start_date: '2026-08-19',
        end_date: '2026-11-19',
        series: [
          {
            role: 'l2_industry',
            name: '保险Ⅱ',
            index_code: '801194.SL',
            expected_sessions: 61,
            qualified_sessions: 61,
            observations: [
              { date: '2026-08-19', close: 1e-40 },
              { date: '2026-11-19', close: 1000 },
            ],
            period_return_pct: null,
            gaps: ['60 日窗口区间收益超出可安全表示范围（首尾收盘极值组合，'
              + '收益无法以有限数值发布），该序列本窗口收益与曲线留空（合格观察保留）'],
          },
        ],
        gaps: [],
      },
    ];
    render(<SectorFirstScreen analysis={analysis} />);
    // 表格保留合格观测与收益留空说明
    expect(screen.getByText('留空（缺口见下）')).toBeTruthy();
    expect(screen.getByText(/61\/61/)).toBeTruthy();
    expect(screen.getByText(/超出可安全表示范围/)).toBeTruthy();
    // 曲线不渲染（收益留空即无曲线）
    expect(screen.queryByTestId('chart-line-l2_industry')).toBeNull();
  });

  it('keeps curve for shortened-calendar window without series gaps', () => {
    // 期望不足（如日历仅 50/61 天）场景 qualified==expected、series gaps
    // 为空：曲线保留（既有行为不因收益留空而误伤）
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_trend_windows = [
      {
        window_days: 60,
        point_count: 61,
        start_date: '2026-09-25',
        end_date: '2026-11-19',
        series: [
          {
            role: 'l2_industry',
            name: '保险Ⅱ',
            index_code: '801194.SL',
            expected_sessions: 50,
            qualified_sessions: 50,
            observations: [
              { date: '2026-09-25', close: 1000 },
              { date: '2026-11-19', close: 1010 },
            ],
            period_return_pct: null,
            gaps: [],
          },
        ],
        gaps: ['60 日窗口：严格交易日历仅 50/61 个已完成交易日，窗口期望不足（不按自然日补齐），各序列本窗口收益留空'],
      },
    ];
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByTestId('chart-line-l2_industry')).toBeTruthy();
  });

  it('hides curve when normalization overflows to non-finite', () => {
    // 收盘极值组合下 UI 归一化 (close/base)*100 上溢为 Infinity：该序列
    // 不画（避免 Infinity 轴与半截线误读）
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_trend_windows = [
      {
        window_days: 60,
        point_count: 61,
        start_date: '2026-08-19',
        end_date: '2026-11-19',
        series: [
          {
            role: 'l2_industry',
            name: '保险Ⅱ',
            index_code: '801194.SL',
            expected_sessions: 61,
            qualified_sessions: 61,
            observations: [
              { date: '2026-08-19', close: 5e-324 },
              { date: '2026-11-19', close: 1000 },
            ],
            period_return_pct: 1.0,
            gaps: [],
          },
        ],
        gaps: [],
      },
    ];
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.queryByTestId('chart-line-l2_industry')).toBeNull();
  });
});

// issue #104：二级同业表（成交活跃代表 + 全成分成交额名次），数据来自存档。
describe('SectorFirstScreen L2 peers', () => {
  function l2PeersBlock() {
    return {
      eligible: true,
      selection_rule: '同一成分快照按成交额前五 + 目标股去重',
      items: [
        { code: '601898', name: '中国神华', amount: 1.0e9, change_pct: 0.5, is_target: false, as_of_date: '2026-11-19' },
        { code: '600188', name: '兖矿能源', amount: 9.0e8, change_pct: -0.87, is_target: true, as_of_date: '2026-11-19' },
        { code: '601225', name: '陕西煤业', amount: 7.0e8, change_pct: 2.2, is_target: false, as_of_date: '2026-11-19' },
      ],
      target_in_amount_top5: true,
      target_change_pct_rank: null,
      target_amount_rank: 2,
      amount_rank_denominator: 5,
      amount_rank_scope: 'full_constituents',
      amount_rank_evidence: 'iFinD 双接口核验一致：p03473 同日名单 5 只 = ths_constituent_num_index 计数 5（FDATE 20261119 回显一致）',
      amount_unit: '元',
      snapshot_rows: 5,
      rankable_rows: 5,
      valid_quote_rows: 5,
      change_pct_qualified_rows: 3,
      verified_constituent_num: 5,
      sample_size: 3,
      quotes_as_of: '2026-11-19 22:00:46',
      industry_reference: false,
      target_independent_quote: null,
      gaps: [],
    };
  }

  it('renders L2 peers table with amount rank, scope and verification', () => {
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_l2_peers = l2PeersBlock();
    render(<SectorFirstScreen analysis={analysis} />);
    // 表格：成员代码与目标标记
    expect(screen.getByText('600188')).toBeTruthy();
    expect(screen.getByText('兖矿能源')).toBeTruthy();
    expect(screen.getByText('601898')).toBeTruthy();
    // 名次行：范围 + 分母 + 核验证据
    expect(screen.getByText(/第 2 \/ 5/)).toBeTruthy();
    expect(screen.getAllByText(/全成分/).length).toBeGreaterThan(0);
    // 双接口核验一致出现在核验证据行与已验证成分数行（各自独立表达）
    expect(screen.getAllByText(/双接口核验一致/).length).toBe(2);
    // 覆盖统计（issue #104 复审：行情覆盖含已知零，与正额名次分母分开）
    expect(screen.getByText(/观察成分 5/)).toBeTruthy();
    expect(screen.getByText(/有效报价 5 行（含已知零 0，正额可排名 5）/)).toBeTruthy();
    expect(screen.getByText(/正额名次分母 5/)).toBeTruthy();
    // 涨跌幅覆盖与已验证成分数（核验事实独立于名次发布）
    expect(screen.getByText(/有效涨跌幅覆盖 3\/3/)).toBeTruthy();
    expect(screen.getByText(/已验证成分数：5/)).toBeTruthy();
  });

  it('separates quote coverage from rank denominator with known zeros', () => {
    // 复审样例：四正一已知零全覆盖——有效报价 5、正额可排名 4、分母 4
    const analysis = confirmedAnalysis();
    const block = l2PeersBlock();
    (block as Record<string, unknown>).rankable_rows = 4;
    (block as Record<string, unknown>).amount_rank_denominator = 4;
    (analysis as Record<string, unknown>).sector_l2_peers = block;
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByText(/有效报价 5 行（含已知零 1，正额可排名 4）/)).toBeTruthy();
    expect(screen.getByText(/正额名次分母 4/)).toBeTruthy();
  });

  it('falls back to positive-only wording for legacy archives', () => {
    // 旧存档无 valid_quote_rows：不假定未采集的已知零数
    const analysis = confirmedAnalysis();
    const block = l2PeersBlock();
    (block as Record<string, unknown>).valid_quote_rows = null;
    (block as Record<string, unknown>).change_pct_qualified_rows = null;
    (block as Record<string, unknown>).verified_constituent_num = null;
    (analysis as Record<string, unknown>).sector_l2_peers = block;
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByText(/有效报价 5 行（正额口径，未采集已知零数）/)).toBeTruthy();
    expect(screen.queryByText(/有效涨跌幅覆盖/)).toBeNull();
    expect(screen.queryByText(/已验证成分数/)).toBeNull();
  });

  it('shows change-pct coverage when rank is withheld and quote date on independent quote', () => {
    // 复审样例：五个入选、四个合格、目标缺幅——覆盖 4/5 可见；独立行情带日期
    const analysis = confirmedAnalysis();
    const block = l2PeersBlock();
    (block as Record<string, unknown>).sample_size = 5;
    (block as Record<string, unknown>).change_pct_qualified_rows = 4;
    (block as Record<string, unknown>).target_independent_quote = {
      code: '601318', name: '中国平安', amount: 2.81e9,
      change_pct: null, is_target: true, as_of_date: '2026-11-18',
    };
    (analysis as Record<string, unknown>).sector_l2_peers = block;
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByText(/有效涨跌幅覆盖 4\/5/)).toBeTruthy();
    expect(screen.getByText(/行情日期 2026-11-18/)).toBeTruthy();
  });

  it('renders sample scope wording when full universe not granted', () => {
    const analysis = confirmedAnalysis();
    const block = l2PeersBlock();
    (block as Record<string, unknown>).amount_rank_scope = 'qualified_sample';
    (block as Record<string, unknown>).amount_rank_evidence = null;
    (analysis as Record<string, unknown>).sector_l2_peers = block;
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByText(/有效正额样本/)).toBeTruthy();
    // 无全量核验证据行；已验证成分数（独立复核事实）照常展示
    expect(screen.queryByText(/全成分核验/)).toBeNull();
    expect(screen.getByText(/已验证成分数：5/)).toBeTruthy();
  });

  it('renders chain gap when L2 peers block absent', () => {
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_l2_peers = null;
    (analysis as Record<string, unknown>).sector_l2_peers_gaps = [
      '二级同业：取数预算已耗尽，二级成分与全量核验未发起（可选出站，不影响既有事实）',
    ];
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByText(/二级同业未取得/)).toBeTruthy();
    expect(screen.getByText(/预算已耗尽/)).toBeTruthy();
  });

  it('does not render L2 peers section for legacy reports', () => {
    const analysis = confirmedAnalysis();
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.queryByText(/二级同业/)).toBeNull();
    expect(screen.queryByText(/二级同业未取得/)).toBeNull();
  });
});

// issue #103 三轮：归一化边界的可画判定只有一份（buildChartData 的
// drawableRoles），Line/图例与画线缺口说明共用；目录版本随已确认身份带出。
describe('SectorFirstScreen 复审三轮', () => {
  function windowsWith(series: unknown[]) {
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_trend_windows = [
      {
        window_days: 60,
        point_count: 61,
        start_date: '2026-08-19',
        end_date: '2026-11-19',
        series,
        gaps: [],
      },
    ];
    return analysis;
  }

  function l1Normal() {
    return {
      role: 'l1_industry', name: '非银金融', index_code: '801790.SL',
      expected_sessions: 3, qualified_sessions: 3,
      observations: [
        { date: '2026-08-19', close: 2000 },
        { date: '2026-09-01', close: 2001 },
        { date: '2026-11-19', close: 2002 },
      ],
      period_return_pct: 0.1, gaps: [],
    };
  }

  it('isolates non-finite normalization and drops its legend line once', () => {
    // 反例 A：首尾等值（收益 0%）、中间 1e308——归一 (1e308/1)*100 上溢
    // 非有限。收益 0% 正常发布且无源缺口，测试真经过归一化隔离分支：
    // 曲线与图例一并隔离并解释画线缺口，正常 L1 共存保留
    const l2 = {
      role: 'l2_industry', name: '保险Ⅱ', index_code: '801194.SL',
      expected_sessions: 3, qualified_sessions: 3,
      observations: [
        { date: '2026-08-19', close: 1 },
        { date: '2026-09-01', close: 1e308 },
        { date: '2026-11-19', close: 1 },
      ],
      period_return_pct: 0,
      gaps: [],
    };
    render(<SectorFirstScreen analysis={windowsWith([l2, l1Normal()])} />);
    expect(screen.queryByTestId('chart-line-l2_industry')).toBeNull();
    expect(screen.queryByTestId('chart-line-l1_industry')).toBeTruthy();
    // 收益 0% 照常发布；画线缺口说明一次（窗口观测措辞）
    expect(screen.getByText(/0\.00%/)).toBeTruthy();
    expect(screen.getAllByText(/曲线未绘制/).length).toBe(1);
    expect(screen.getByText(/窗口观测收盘极值组合/)).toBeTruthy();
  });

  it('isolates subnormal-to-zero normalization while keeping 0% return', () => {
    // 反例 B：首尾 1e200、中间 1e-200——中间归一值下溢为 0（非正），不作为
    // 真实点绘制；首尾收益 0% 保留（后端正常发布），正常 L1 共存
    const l2 = {
      role: 'l2_industry', name: '保险Ⅱ', index_code: '801194.SL',
      expected_sessions: 3, qualified_sessions: 3,
      observations: [
        { date: '2026-08-19', close: 1e200 },
        { date: '2026-09-01', close: 1e-200 },
        { date: '2026-11-19', close: 1e200 },
      ],
      period_return_pct: 0,
      gaps: [],
    };
    render(<SectorFirstScreen analysis={windowsWith([l2, l1Normal()])} />);
    expect(screen.queryByTestId('chart-line-l2_industry')).toBeNull();
    expect(screen.queryByTestId('chart-line-l1_industry')).toBeTruthy();
    // 收益照常发布（0%），画线缺口说明解释隔离原因
    expect(screen.getByText(/0\.00%/)).toBeTruthy();
    expect(screen.getAllByText(/曲线未绘制/).length).toBe(1);
    expect(screen.getByText(/窗口观测收盘极值组合/)).toBeTruthy();
  });

  it('shows directory version on confirmed L2 identity', () => {
    // 速览节跳过后目录版本在已确认身份行带出（含核验口径）
    render(<SectorFirstScreen analysis={confirmedAnalysis()} />);
    expect(screen.getByText(/目录版本 sw2021-l2-20261119/)).toBeTruthy();
    expect(screen.getByText(/官方发布目录 \+ iFinD 核验子集/)).toBeTruthy();
  });
});

// issue #103 五轮：正收盘比值中间除法下溢为 0 时用先乘 100 再除的等价
// 计算——混合序列可表示点保留、无假画线缺口；真不可表示仍禁画。
describe('SectorFirstScreen 复审五轮', () => {
  it('keeps mixed series drawable via equivalent normalization', () => {
    // 确认例：首尾均 3、中间 Number.MIN_VALUE=5e-324——(5e-324/3)*100 下溢
    // 为 0 曾把整序列隔离；等价式 (5e-324*100)/3 ≈ 1.67e-322 可表示 →
    // 曲线照常、无画线缺口说明
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_trend_windows = [
      {
        window_days: 60,
        point_count: 61,
        start_date: '2026-08-19',
        end_date: '2026-11-19',
        series: [
          {
            role: 'l2_industry',
            name: '保险Ⅱ',
            index_code: '801194.SL',
            expected_sessions: 3,
            qualified_sessions: 3,
            observations: [
              { date: '2026-08-19', close: 3 },
              { date: '2026-09-01', close: Number.MIN_VALUE },
              { date: '2026-11-19', close: 3 },
            ],
            period_return_pct: 0,
            gaps: [],
          },
        ],
        gaps: [],
      },
    ];
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.getByTestId('chart-line-l2_industry')).toBeTruthy();
    expect(screen.queryByText(/曲线未绘制/)).toBeNull();
    // 收益 0% 照常发布
    expect(screen.getByText(/0\.00%/)).toBeTruthy();
  });

  it('still hides truly unrepresentable normalization after equivalence retry', () => {
    // 等价式仍下溢（1e-198/1e200 = 1e-398 < MIN_VALUE）：真不可表示保持禁画
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_trend_windows = [
      {
        window_days: 60,
        point_count: 61,
        start_date: '2026-08-19',
        end_date: '2026-11-19',
        series: [
          {
            role: 'l2_industry',
            name: '保险Ⅱ',
            index_code: '801194.SL',
            expected_sessions: 3,
            qualified_sessions: 3,
            observations: [
              { date: '2026-08-19', close: 1e200 },
              { date: '2026-09-01', close: 1e-200 },
              { date: '2026-11-19', close: 1e200 },
            ],
            period_return_pct: 0,
            gaps: [],
          },
        ],
        gaps: [],
      },
    ];
    render(<SectorFirstScreen analysis={analysis} />);
    expect(screen.queryByTestId('chart-line-l2_industry')).toBeNull();
    expect(screen.getAllByText(/曲线未绘制/).length).toBe(1);
  });
});

// issue #104 六轮：正但不足 1 元的金额保留原值字面（String(v)），与真零 0
// 有清楚区别——服务端仍按正额排行，页面不得把小正数显示成「0」。
describe('SectorFirstScreen 复审六轮', () => {
  it('distinguishes sub-1-yuan positive amounts from true zero', () => {
    const analysis = confirmedAnalysis();
    (analysis as Record<string, unknown>).sector_l2_peers = {
      eligible: true,
      selection_rule: '同一成分快照按成交额前五 + 目标股去重',
      items: [
        { code: '601898', name: '中国神华', amount: 0.1, change_pct: 0.5, is_target: false, as_of_date: '2026-11-19' },
        { code: '600188', name: '兖矿能源', amount: 0, change_pct: 0.5, is_target: false, as_of_date: '2026-11-19' },
        { code: '601225', name: '陕西煤业', amount: 7.0e8, change_pct: 2.2, is_target: false, as_of_date: '2026-11-19' },
      ],
      target_in_amount_top5: false,
      target_change_pct_rank: null,
      target_amount_rank: 3,
      amount_rank_denominator: 3,
      amount_rank_scope: 'full_constituents',
      amount_rank_evidence: 'iFinD 双接口核验一致',
      amount_unit: '元',
      snapshot_rows: 3,
      rankable_rows: 2,
      valid_quote_rows: 3,
      change_pct_qualified_rows: 3,
      verified_constituent_num: 3,
      sample_size: 3,
      quotes_as_of: null,
      industry_reference: false,
      target_independent_quote: {
        code: '601318', name: '中国平安', amount: 0.1,
        change_pct: null, is_target: true, as_of_date: '2026-11-18',
      },
      gaps: [],
    };
    render(<SectorFirstScreen analysis={analysis} />);
    // 小正数保留字面 0.1（表格与独立行情同一 formatter），真零显示 0
    expect(screen.getAllByText('0.1').length).toBe(1);  // 表格单元格
    expect(screen.getByText(/成交额 0\.1 元/)).toBeTruthy();  // 独立行情（同 formatter）
    expect(screen.getByText('0')).toBeTruthy();          // 真零不受影响
    // 可表示极小值同样保留字面（String(v)）
    expect(screen.getByText(/成交额（元）/)).toBeTruthy();
  });
});
