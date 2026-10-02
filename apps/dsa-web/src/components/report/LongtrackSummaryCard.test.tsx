import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { LongtrackSummaryCard } from './LongtrackSummaryCard';
import type { AnalysisReport } from '../../types/analysis';

const navigateMock = vi.fn();

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => navigateMock };
});

function makeReport(overrides: Partial<AnalysisReport> = {}): AnalysisReport {
  return {
    meta: { id: 44, stockCode: '600519', stockName: '贵州茅台', reportType: 'simple' },
    summary: {},
    researchFramework: {
      dimensionTotal: 54.13,
      version: 'v2.0',
      dimensions: [
        { dimension: '基本面', weight: 0.3, score: 60 },
        { dimension: '消息面', weight: 0.15, score: 54 },
      ],
    },
    investmentConclusion: { action: '观察', edge: -0.21 },
    ...overrides,
  } as AnalysisReport;
}

describe('LongtrackSummaryCard', () => {
  it('renders score, action, dimension chips and jump button', () => {
    render(
      <MemoryRouter>
        <LongtrackSummaryCard report={makeReport()} />
      </MemoryRouter>,
    );
    expect(screen.getByText('投研总评分')).toBeInTheDocument();
    expect(screen.getByText(/54\.1/)).toBeInTheDocument();
    expect(screen.getByText('观察')).toBeInTheDocument();
    expect(screen.getByText('基本面 60')).toBeInTheDocument();
    expect(screen.getByText('v2.0')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /查看完整深度报告/ }));
    expect(navigateMock).toHaveBeenCalledWith(
      '/deep-research?code=600519&name=%E8%B4%B5%E5%B7%9E%E8%8C%85%E5%8F%B0',
    );
  });

  it('renders nothing when researchFramework is missing (legacy records)', () => {
    const { container } = render(
      <MemoryRouter>
        <LongtrackSummaryCard report={makeReport({ researchFramework: undefined })} />
      </MemoryRouter>,
    );
    expect(container.firstChild).toBeNull();
  });
});
