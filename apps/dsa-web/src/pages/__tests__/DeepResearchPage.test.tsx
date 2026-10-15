import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { state } = vi.hoisted(() => ({ state: { current: {} as Record<string, unknown> } }));
vi.mock('../../hooks/useDeepResearch', async (original) => ({
  ...await original<typeof import('../../hooks/useDeepResearch')>(),
  useDeepResearch: () => state.current,
}));
vi.mock('../../api/deepResearch', () => ({ deepResearchApi: {
  getReports: vi.fn().mockResolvedValue([]),
  dimReportUrl: (id: string, dim: string) => `/reports/${id}/${dim}`,
} }));
vi.mock('../../components/StockAutocomplete/StockAutocomplete', () => ({ StockAutocomplete: () => <input aria-label="股票" /> }));
import { DUAL_TRACK_DIMS } from '../../hooks/useDeepResearch';
import { DeepResearchPage } from '../DeepResearchPage';

afterEach(cleanup);
function ready(dimensions?: Record<string, { status: string }>, engine = 'dual_track') {
  state.current = {
    status: 'done', progressSteps: [], reportId: '300260_202610151900', error: null, dimStatuses: {},
    report: { id: '300260_202610151900', engine, markdown: '# 已取得的资料', quality_score: 64, missing_layers: ['business'] },
    dualTrack: { dimensions, guardrailEvents: [], dimsDegraded: ['business'] }, generate: vi.fn(), cancel: vi.fn(), reset: vi.fn(),
  };
}

describe('research presentation', () => {
  it('shows real completed counts and Chinese chapter names without claiming data accuracy', () => {
    ready(Object.fromEntries(DUAL_TRACK_DIMS.map(({ id }) => [id, { status: id === 'business' ? 'degraded' : id === 'ownership' ? 'skipped' : 'ok' }])));
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
});
