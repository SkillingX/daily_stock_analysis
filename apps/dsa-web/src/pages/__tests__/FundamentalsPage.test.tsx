import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import FundamentalsPage from '../FundamentalsPage';

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../../api/index', () => ({ default: { get, post } }));
vi.mock('../../components/StockAutocomplete/StockAutocomplete', () => ({
  StockAutocomplete: ({ onSubmit }: { onSubmit: (code: string, name: string) => void }) =>
    <button onClick={() => onSubmit('600519', '样本')}>选择样本</button>,
}));
vi.mock('../../components/report/ReportMarkdownBody', () => ({
  ReportMarkdownBody: ({ content }: { content: string }) => <div>{content}</div>,
}));

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue({ data: { data: [] } });
});

describe('Fundamentals refresh intent', () => {
  it('blocks duplicate in-flight refresh and treats a later click as a new intent', async () => {
    let resolve!: (value: unknown) => void;
    post.mockReturnValue(new Promise((done) => { resolve = done; }));
    render(<FundamentalsPage />);
    fireEvent.click(screen.getByText('选择样本'));
    const refresh = screen.getByRole('button', { name: '刷新数据并生成新报告' });
    act(() => { fireEvent.click(refresh); fireEvent.click(refresh); });
    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0][0]).toContain('force_refresh=true');
    expect(screen.getByText('刷新财务数据中...')).toBeInTheDocument();
    await act(async () => { resolve({ data: { report_id: 'fd_202610091200_1', markdown: 'partial 财务品质' } }); });
    expect(screen.getByText(/新报告已保存/)).toBeInTheDocument();
    post.mockResolvedValue({ data: { report_id: 'fd_202610091200_2', markdown: 'partial 新版本' } });
    fireEvent.click(screen.getByRole('button', { name: '刷新数据并生成新报告' }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  });

  it('keeps the displayed old report on POST failure and does not retry', async () => {
    post.mockResolvedValueOnce({ data: { report_id: 'fd_202610091200', markdown: '已有报告' } });
    render(<FundamentalsPage />);
    fireEvent.click(screen.getByText('选择样本'));
    fireEvent.click(screen.getByRole('button', { name: '生成报告' }));
    await screen.findByText('已有报告');
    expect(post.mock.calls[0][0]).toContain('force_refresh=false');
    post.mockRejectedValueOnce(new Error('受控错误'));
    fireEvent.click(screen.getByRole('button', { name: '刷新数据并生成新报告' }));
    await screen.findByText(/操作不会自动重试/);
    expect(screen.getByText('已有报告')).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(2);
  });
});
