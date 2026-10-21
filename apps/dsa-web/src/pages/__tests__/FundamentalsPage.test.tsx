import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import FundamentalsPage from '../FundamentalsPage';
import { AxiosError } from 'axios';

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
    expect(post.mock.calls[0][2]).toEqual({ timeout: 330000 });
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
  it('shows saved partial results without claiming the report is complete', async () => {
    post.mockResolvedValue({ data: { report_id: 'fd_202610121800_1', status: 'partial', markdown: '业务画像未完成；财务数据已保留' } });
    render(<FundamentalsPage />);
    fireEvent.click(screen.getByText('选择样本'));
    fireEvent.click(screen.getByRole('button', { name: '生成报告' }));
    await screen.findByText(/已保存部分报告/);
    expect(screen.getByText('业务画像未完成；财务数据已保留')).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
  });

  it('refreshes history once after client timeout without retrying or adopting an unrelated report', async () => {
    get.mockResolvedValueOnce({ data: { data: [] } });
    post.mockRejectedValueOnce(new AxiosError('timeout of 330000ms exceeded', 'ECONNABORTED'));
    render(<FundamentalsPage />);
    fireEvent.click(screen.getByText('选择样本'));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    get.mockResolvedValueOnce({ data: { data: [{ id: 'fd_202610211200', stock_code: '600519', stock_name: '另一次请求的报告' }] } });
    fireEvent.click(screen.getByRole('button', { name: '生成报告' }));
    await screen.findByText(/本次等待已结束/);
    expect(screen.getByText('另一次请求的报告')).toBeInTheDocument();
    expect(screen.getByText('输入股票，生成经营模式/主营产品/行业地位/龙头对比的详细基本面报告')).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it('does not report generation failure when only history refresh fails', async () => {
    render(<FundamentalsPage />);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    get.mockRejectedValueOnce(new Error('历史列表暂不可用'));
    post.mockResolvedValueOnce({ data: { report_id: 'fd_202610211200', status: 'partial', markdown: '已经保存的部分报告' } });
    fireEvent.click(screen.getByText('选择样本'));
    fireEvent.click(screen.getByRole('button', { name: '生成报告' }));
    await screen.findByText('已经保存的部分报告');
    expect(screen.getByText(/已保存部分报告/)).toBeInTheDocument();
    expect(screen.getByText(/历史列表刷新失败/)).toBeInTheDocument();
    expect(screen.queryByText(/操作不会自动重试/)).not.toBeInTheDocument();
  });

  it('keeps a server timeout distinct when the recovery history request also fails', async () => {
    render(<FundamentalsPage />);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    get.mockRejectedValueOnce(new Error('history unavailable'));
    post.mockRejectedValueOnce(Object.assign(new AxiosError('服务端模型请求超时', 'ECONNABORTED'), { response: { status: 504 } }));
    fireEvent.click(screen.getByText('选择样本'));
    fireEvent.click(screen.getByRole('button', { name: '生成报告' }));
    await screen.findByText(/服务端模型请求超时/);
    await screen.findByText(/历史列表刷新失败/);
    expect(screen.queryByText(/本次等待已结束/)).not.toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenCalledTimes(1);
  });

});
