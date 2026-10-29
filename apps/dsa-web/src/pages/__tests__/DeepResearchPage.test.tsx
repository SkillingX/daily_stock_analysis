import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { state } = vi.hoisted(() => ({ state: { current: {} as Record<string, unknown> } }));
const { getReports, getReport, getDims } = vi.hoisted(() => ({
  getReports: vi.fn().mockResolvedValue([]),
  getReport: vi.fn(),
  getDims: vi.fn(),
}));
vi.mock('../../hooks/useDeepResearch', async (original) => ({
  ...await original<typeof import('../../hooks/useDeepResearch')>(),
  useDeepResearch: () => state.current,
}));
vi.mock('../../api/deepResearch', () => ({ deepResearchApi: {
  getReports, getReport, getDims,
  dimReportUrl: (id: string, dim: string) => `/reports/${id}/${dim}`,
} }));
vi.mock('../../components/StockAutocomplete/StockAutocomplete', () => ({ StockAutocomplete: () => <input aria-label="股票" /> }));
import { ACTIVE_DUAL_TRACK_DIMS, DUAL_TRACK_DIMS } from '../../hooks/useDeepResearch';
import { DeepResearchPage } from '../DeepResearchPage';

afterEach(cleanup);

const REPORT_ID = '300260_202610151900';

function ready(
  dimensions?: Record<string, { status: string }>,
  engine = 'dual_track',
  extra: { report?: Record<string, unknown>; dimsDegraded?: string[] } = {},
) {
  state.current = {
    status: 'done', progressSteps: [], reportId: REPORT_ID, error: null, dimStatuses: {},
    report: { id: REPORT_ID, engine, markdown: '# 已取得的资料', quality_score: 64, missing_layers: ['business'], ...extra.report },
    dualTrack: { dimensions, guardrailEvents: [], dimsDegraded: extra.dimsDegraded ?? ['business'] }, generate: vi.fn(), cancel: vi.fn(), reset: vi.fn(),
  };
}

/** 全维度同构 payload（历史 19 项目录），special 覆盖个别状态 */
const allDims = (special: Record<string, string>) =>
  Object.fromEntries(DUAL_TRACK_DIMS.map(({ id }) => [id, { status: special[id] ?? 'ok' }]));

describe('research presentation', () => {
  it('shows real completed counts and Chinese chapter names without claiming data accuracy', () => {
    ready(allDims({ business: 'degraded', ownership: 'skipped' }));
    render(<DeepResearchPage />);
    expect(screen.getByText('本次研究：正常 17 项 · 未完整完成 1 项 · 主动未选 1 项')).toBeTruthy();
    expect(screen.getByText('待补充：业务')).toBeTruthy();
    expect(screen.getByText(/章节状态表示本次研究进度/)).toBeTruthy();
    expect(screen.queryByText(/结构校验评分/)).toBeNull();
    expect(screen.getByText(/分析范围：/)).toBeTruthy();
    expect(screen.queryByText(/省钱模式/)).toBeNull();
  });
  it('does not infer completion counts from absent historical dimension states or legacy scores', () => {
    ready(undefined);
    const { unmount } = render(<DeepResearchPage />);
    expect(screen.getByText(/历史章节状态记录不完整/)).toBeTruthy();
    expect(screen.queryByText(/正常 18 项/)).toBeNull();
    unmount();
    ready(undefined, 'legacy');
    render(<DeepResearchPage />);
    expect(screen.getByText('结构校验评分：64/100（非数据准确率）')).toBeTruthy();
    expect(screen.queryByText(/本次研究：正常/)).toBeNull();
  });

  it('shows generation progress badges for the current 18 active dims only', () => {
    state.current = {
      status: 'generating', progressSteps: [], reportId: null, error: null,
      dimStatuses: Object.fromEntries(ACTIVE_DUAL_TRACK_DIMS.map(({ id }) => [id, 'pending'])),
      report: null, dualTrack: { guardrailEvents: [], dimsDegraded: [] },
      generate: vi.fn(), cancel: vi.fn(), reset: vi.fn(),
    };
    render(<DeepResearchPage />);
    expect(screen.getAllByText('财务').length).toBeGreaterThan(0);
    // 退役 ownership 无 pending 徽章，也不出现在选择集
    expect(screen.queryByText('股权')).toBeNull();
  });

  it('validates completion counts against the current 18 for new reports without ownership', () => {
    const dims = Object.fromEntries(
      ACTIVE_DUAL_TRACK_DIMS.map(({ id }) => [id, { status: id === 'capital' ? 'degraded' : 'ok' }]),
    );
    ready(dims, 'dual_track', { dimsDegraded: ['capital'] });
    render(<DeepResearchPage />);
    expect(screen.getByText('本次研究：正常 17 项 · 未完整完成 1 项 · 主动未选 0 项')).toBeTruthy();
    expect(screen.getByText('待补充：资金')).toBeTruthy();
    // 旧 payload 无可用性字段 → 不推测子报告链接
    expect(screen.queryByText('子报告：')).toBeNull();
  });

  it('keeps saved historical ownership partial state visible under the 19-dim contract', () => {
    ready(allDims({ ownership: 'degraded' }), 'dual_track', {
      dimsDegraded: ['ownership'],
      report: { available_dim_reports: ['ownership'] },
    });
    render(<DeepResearchPage />);
    expect(screen.getByText('本次研究：正常 18 项 · 未完整完成 1 项 · 主动未选 0 项')).toBeTruthy();
    expect(screen.getByText('待补充：股权')).toBeTruthy();
    // 历史 ownership 实际产物文件存在 → 股权子报告链接可见
    expect(screen.getByRole('link', { name: '股权' }).getAttribute('href')).toBe(`/reports/${REPORT_ID}/ownership`);
    expect(screen.getByRole('link', { name: '下载股权子报告' })).toBeTruthy();
  });

  it('does not count one-dimension fragments as complete or erase persisted failures', () => {
    ready({ six_dim: { status: 'degraded' } }, 'dual_track', { dimsDegraded: ['six_dim'] });
    render(<DeepResearchPage />);
    expect(screen.getByText(/历史章节状态记录不完整/)).toBeTruthy();
    expect(screen.getByText('待补充：六维')).toBeTruthy();
    expect(screen.queryByText(/本次研究：正常/)).toBeNull();
  });

  it('lists subreport links only for backend-confirmed artifacts and never falls back to the catalogue', () => {
    ready(allDims({}), 'dual_track', {
      dimsDegraded: [],
      report: { available_dim_reports: ['fundamental', 'ownership', 'mystery_dim'] },
    });
    render(<DeepResearchPage />);
    expect(screen.getByRole('link', { name: '财务' }).getAttribute('href')).toBe(`/reports/${REPORT_ID}/fundamental`);
    expect(screen.getByRole('link', { name: '股权' })).toBeTruthy();
    // 目录中其余维度（如板块）无实际产物 → 无链接；未知 id 不渲染
    expect(screen.queryByRole('link', { name: '板块' })).toBeNull();
    expect(screen.queryByText('mystery_dim')).toBeNull();
  });

  it('renders no speculative subreport links when availability is empty', () => {
    ready(allDims({}), 'dual_track', { dimsDegraded: [], report: { available_dim_reports: [] } });
    render(<DeepResearchPage />);
    expect(screen.queryByText('子报告：')).toBeNull();
  });

  it('preserves availability through history selection including historical ownership', async () => {
    getReport.mockResolvedValue({
      id: '600519_2025', stock_code: '600519', stock_name: '贵州茅台',
      markdown: '# 旧报告', engine: 'dual_track', available_dim_reports: ['ownership'],
    });
    getDims.mockResolvedValue({ guardrail_events: [], dimensions: allDims({ ownership: 'degraded' }) });
    getReports.mockResolvedValue([
      { id: '600519_2025', stock_code: '600519', stock_name: '贵州茅台', status: 'partial', missing_layers: ['ownership'] },
    ]);
    state.current = {
      status: 'idle', progressSteps: [], reportId: null, error: null, dimStatuses: {},
      report: null, dualTrack: { guardrailEvents: [], dimsDegraded: [] },
      generate: vi.fn(), cancel: vi.fn(), reset: vi.fn(),
    };
    render(<DeepResearchPage />);
    fireEvent.click(await screen.findByText('贵州茅台'));
    await waitFor(() => expect(screen.getByText('子报告：')).toBeTruthy());
    expect(screen.getByRole('link', { name: '股权' }).getAttribute('href')).toBe('/reports/600519_2025/ownership');
    // 历史 19 项 payload（含 ownership degraded）按 19 口径给出可靠计数
    expect(screen.getByText('本次研究：正常 18 项 · 未完整完成 1 项 · 主动未选 0 项')).toBeTruthy();
  });
});
