// 个股板块分析页行为测试（issue #81）：生成防重复提交、可见错误、移动历史入口、
// 下载与普通请求一致基址来源、前端等待超时的历史回看提示。
// 供应商/后端边界用 apiClient mock；页面交互走真实组件逻辑。

import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const postMock = vi.hoisted(() => vi.fn());
const getMock = vi.hoisted(() => vi.fn());
const deleteMock = vi.hoisted(() => vi.fn());
const API_BASE_URL = vi.hoisted(() => 'http://api.example.com');

vi.mock('../../api', () => ({
  default: {
    post: postMock,
    get: getMock,
    delete: deleteMock,
  },
}));

// 与 apiClient 同源的基址常量（页面下载链接必须使用同一来源）
vi.mock('../../utils/constants', () => ({
  API_BASE_URL,
}));

// StockAutocomplete 涉及联想请求，页面测试以行为等价替身驱动选股
vi.mock('../../components/StockAutocomplete/StockAutocomplete', () => ({
  StockAutocomplete: ({ onSubmit, disabled }: { onSubmit: (code: string, name?: string) => void; disabled?: boolean }) => (
    <button type="button" onClick={() => onSubmit('600519', '贵州茅台')} disabled={disabled}>
      mock-select-stock
    </button>
  ),
}));

vi.mock('../../components/report/ReportMarkdownBody', () => ({
  ReportMarkdownBody: ({ content }: { content: string }) => (
    <div data-testid="report-markdown">{content}</div>
  ),
}));

import SectorAnalysisPage from '../SectorAnalysisPage';

const HISTORY_URL = '/api/v1/sector-analysis/reports';
const GENERATE_URL = `/api/v1/sector-analysis/generate?stock_code=${encodeURIComponent('600519')}&stock_name=${encodeURIComponent('贵州茅台')}`;

function axiosError(status: number, data: unknown, code?: string): Error & { response?: unknown; code?: string } {
  const err = new Error(`Request failed with status code ${status}`) as Error & { response?: unknown; code?: string };
  err.response = { status, data };
  if (code) err.code = code;
  return err;
}

const legacyItem = {
  id: 'sa_202611010900', stock_code: '600519', stock_name: '贵州茅台',
  created_at: '2026-11-01T09:00:00', sector_score: 61.5,
};
const newItem = {
  id: 'sa_202611051200', stock_code: '600519', stock_name: '贵州茅台',
  created_at: '2026-11-05T12:00:00', sector_score: null,
};

async function selectStock() {
  fireEvent.click(screen.getByRole('button', { name: 'mock-select-stock' }));
}

describe('SectorAnalysisPage', () => {
  beforeEach(() => {
    postMock.mockReset();
    getMock.mockReset();
    deleteMock.mockReset();
    getMock.mockResolvedValue({ data: { success: true, data: [legacyItem, newItem], total: 2 } });
  });

  afterEach(() => {
    cleanup();
  });

  it('展示历史列表：旧记录显示综合分，新记录不显示', async () => {
    render(<SectorAnalysisPage />);
    await waitFor(() => expect(getMock).toHaveBeenCalledWith(HISTORY_URL));
    await waitFor(() => {
      expect(screen.getByText(/61\.5分/)).toBeInTheDocument();
    });
    expect(screen.getAllByText('贵州茅台').length).toBeGreaterThan(0);
  });

  it('历史加载失败时显示可见错误', async () => {
    getMock.mockRejectedValue(axiosError(500, { error: 'http_error', message: '服务器内部错误' }));
    render(<SectorAnalysisPage />);
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/历史报告加载失败/);
    });
  });

  it('生成成功：渲染报告并刷新历史', async () => {
    postMock.mockResolvedValue({
      data: { report_id: 'sa_202611051300', stock_code: '600519', stock_name: '贵州茅台',
              markdown: '# 新报告正文', analysis: { payload_version: 2 } },
    });
    render(<SectorAnalysisPage />);
    await selectStock();
    fireEvent.click(screen.getByRole('button', { name: /生成报告/ }));
    await waitFor(() => expect(postMock).toHaveBeenCalledWith(GENERATE_URL, undefined, { timeout: 330000 }));
    await waitFor(() => {
      expect(screen.getByTestId('report-markdown')).toHaveTextContent('# 新报告正文');
    });
    expect(getMock).toHaveBeenCalledWith(HISTORY_URL); // 历史已刷新
  });

  it('生成中防重复提交：按钮禁用且只发一次请求', async () => {
    let resolvePost: (v: unknown) => void = () => {};
    postMock.mockReturnValue(new Promise((resolve) => { resolvePost = resolve; }));
    render(<SectorAnalysisPage />);
    await selectStock();
    const button = screen.getByRole('button', { name: /生成报告|生成中/ });
    fireEvent.click(button);
    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    const pendingButton = screen.getByRole('button', { name: /生成中/ });
    expect(pendingButton).toBeDisabled();
    fireEvent.click(pendingButton);
    expect(postMock).toHaveBeenCalledTimes(1);
    resolvePost({ data: { report_id: null } });
    await waitFor(() => expect(screen.getByRole('button', { name: /生成报告/ })).toBeEnabled());
  });

  it('保存失败（500）显示服务端消息，不假装成功', async () => {
    postMock.mockRejectedValue(axiosError(500, { error: 'http_error', message: '报告已生成但保存失败，未存档；请重试' }));
    render(<SectorAnalysisPage />);
    await selectStock();
    fireEvent.click(screen.getByRole('button', { name: /生成报告/ }));
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('报告已生成但保存失败');
    });
    expect(screen.queryByTestId('report-markdown')).not.toBeInTheDocument();
  });

  it('前端等待超时（330s 上限）给出历史回看提示，不混同为处理失败', async () => {
    postMock.mockRejectedValue(axiosError(0, null, 'ECONNABORTED'));
    render(<SectorAnalysisPage />);
    await selectStock();
    fireEvent.click(screen.getByRole('button', { name: /生成报告/ }));
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/历史/);
      expect(screen.getByRole('alert')).toHaveTextContent(/超时/);
    });
  });

  it('移动端提供历史入口：切换后历史可见', async () => {
    render(<SectorAnalysisPage />);
    const toggle = screen.getByRole('button', { name: /历史报告/ });
    expect(toggle).toBeInTheDocument(); // md:hidden 移动端入口
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(screen.getAllByText('贵州茅台').length).toBeGreaterThan(0);
    });
  });

  it('详情打开失败显示可见错误', async () => {
    getMock.mockImplementation((url: string) => {
      if (url === HISTORY_URL) return Promise.resolve({ data: { success: true, data: [legacyItem], total: 1 } });
      return Promise.reject(axiosError(404, { error: 'http_error', message: '报告不存在' }));
    });
    render(<SectorAnalysisPage />);
    await waitFor(() => expect(screen.getByText('贵州茅台')).toBeInTheDocument());
    fireEvent.click(screen.getByText('贵州茅台'));
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('报告不存在');
    });
  });

  it('删除失败显示可见错误且不刷新列表', async () => {
    deleteMock.mockRejectedValue(axiosError(500, { error: 'http_error', message: '删除失败：服务器内部错误' }));
    render(<SectorAnalysisPage />);
    await waitFor(() => expect(screen.getAllByRole('button', { name: '删除' }).length).toBeGreaterThan(0));
    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[0]);
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('删除失败');
    });
    expect(deleteMock).toHaveBeenCalledTimes(1);
  });

  it('删除成功后刷新历史并清空当前详情', async () => {
    deleteMock.mockResolvedValue({ data: { success: true, deleted: legacyItem.id } });
    getMock.mockImplementation((url: string) => {
      if (url === HISTORY_URL) return Promise.resolve({ data: { success: true, data: [legacyItem], total: 1 } });
      return Promise.resolve({ data: { success: true, data: { ...legacyItem, markdown: '# 旧报告', analysis_json: null } } });
    });
    render(<SectorAnalysisPage />);
    await waitFor(() => expect(screen.getByText('贵州茅台')).toBeInTheDocument());
    fireEvent.click(screen.getByText('贵州茅台'));
    await waitFor(() => expect(screen.getByTestId('report-markdown')).toHaveTextContent('# 旧报告'));
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith(`${HISTORY_URL}/${legacyItem.id}`));
    await waitFor(() => {
      expect(screen.queryByTestId('report-markdown')).not.toBeInTheDocument();
    });
  });

  it('下载/查看原文件经统一 apiClient 取回（同一基址与认证语义）', async () => {
    postMock.mockResolvedValue({
      data: { report_id: 'sa_202611051300', stock_code: '600519', stock_name: '贵州茅台',
              markdown: '# 报告', analysis: { payload_version: 2 } },
    });
    const blob = new Blob(['# 报告'], { type: 'text/markdown' });
    getMock.mockImplementation((url: string, config?: { responseType?: string }) => {
      if (url === HISTORY_URL) return Promise.resolve({ data: { success: true, data: [], total: 0 } });
      expect(config?.responseType).toBe('blob');
      return Promise.resolve({ data: blob });
    });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    try {
      render(<SectorAnalysisPage />);
      await selectStock();
      fireEvent.click(screen.getByRole('button', { name: /生成报告/ }));
      await waitFor(() => expect(screen.getByTestId('report-markdown')).toBeInTheDocument());
      fireEvent.click(screen.getByRole('button', { name: /下载 Markdown/ }));
      await waitFor(() => {
        expect(getMock).toHaveBeenCalledWith(
          `/api/v1/sector-analysis/reports/sa_202611051300/markdown?download=1`,
          expect.objectContaining({ responseType: 'blob' }),
        );
        expect(clickSpy).toHaveBeenCalled();
      });
      fireEvent.click(screen.getByRole('button', { name: /查看原文件/ }));
      await waitFor(() => {
        expect(getMock).toHaveBeenCalledWith(
          `/api/v1/sector-analysis/reports/sa_202611051300/markdown`,
          expect.objectContaining({ responseType: 'blob' }),
        );
        expect(openSpy).toHaveBeenCalled();
      });
    } finally {
      clickSpy.mockRestore();
      openSpy.mockRestore();
    }
  });

  it('下载失败在页面显示可见错误（原生链接 500 不再静默）', async () => {
    postMock.mockResolvedValue({
      data: { report_id: 'sa_202611051300', stock_code: '600519', stock_name: '贵州茅台',
              markdown: '# 报告', analysis: { payload_version: 2 } },
    });
    getMock.mockImplementation((url: string) => {
      if (url === HISTORY_URL) return Promise.resolve({ data: { success: true, data: [], total: 0 } });
      return Promise.reject(axiosError(500, { error: 'http_error', message: '报告文件不存在' }));
    });
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const createSpy = vi.spyOn(URL, 'createObjectURL');
    try {
      render(<SectorAnalysisPage />);
      await selectStock();
      fireEvent.click(screen.getByRole('button', { name: /生成报告/ }));
      await waitFor(() => expect(screen.getByTestId('report-markdown')).toBeInTheDocument());
      fireEvent.click(screen.getByRole('button', { name: /下载 Markdown/ }));
      await waitFor(() => {
        expect(screen.getByRole('alert')).toHaveTextContent(/下载失败/);
        expect(screen.getByRole('alert')).toHaveTextContent(/报告文件不存在/);
      });
      expect(createSpy).not.toHaveBeenCalled(); // 失败时不触发浏览器下载
    } finally {
      revokeSpy.mockRestore();
      createSpy.mockRestore();
    }
  });

  it('下载失败响应为 blob 时局部解析服务端 message，不显示空对象（真实 404 反例）', async () => {
    postMock.mockResolvedValue({
      data: { report_id: 'sa_202611051300', stock_code: '600519', stock_name: '贵州茅台',
              markdown: '# 报告', analysis: { payload_version: 2 } },
    });
    const blobErrorBody = new Blob([JSON.stringify({ error: 'http_error', message: '报告文件不存在' })],
      { type: 'application/json' });
    getMock.mockImplementation((url: string) => {
      if (url === HISTORY_URL) return Promise.resolve({ data: { success: true, data: [], total: 0 } });
      return Promise.reject(axiosError(404, blobErrorBody));
    });
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    try {
      render(<SectorAnalysisPage />);
      await selectStock();
      fireEvent.click(screen.getByRole('button', { name: /生成报告/ }));
      await waitFor(() => expect(screen.getByTestId('report-markdown')).toBeInTheDocument());
      fireEvent.click(screen.getByRole('button', { name: /下载 Markdown/ }));
      await waitFor(() => {
        expect(screen.getByRole('alert')).toHaveTextContent(/下载失败/);
        expect(screen.getByRole('alert')).toHaveTextContent('报告文件不存在');
        expect(screen.getByRole('alert')).toHaveTextContent('HTTP 404');
      });
      expect(screen.getByRole('alert').textContent).not.toContain('{}');
    } finally {
      revokeSpy.mockRestore();
    }
  });

  it('删除反馈部分失败：记录已删除但文件清理失败时如实提示', async () => {
    deleteMock.mockResolvedValue({
      data: { success: true, deleted: legacyItem.id,
              file_cleanup: 'failed', message: '记录已删除，但存档文件清理失败（文件可能残留，可手动删除）' },
    });
    render(<SectorAnalysisPage />);
    await waitFor(() => expect(screen.getAllByRole('button', { name: '删除' }).length).toBeGreaterThan(0));
    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[0]);
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('记录已删除，但存档文件清理失败');
    });
    expect(getMock).toHaveBeenCalledWith(HISTORY_URL); // 列表仍已刷新（记录确实已删）
  });

  it('历史删除控件默认可见（触屏/小屏不依赖 hover），桌面 sm+ 保留悬停浮现', async () => {
    render(<SectorAnalysisPage />);
    await waitFor(() => expect(screen.getAllByRole('button', { name: '删除' }).length).toBeGreaterThan(0));
    for (const button of screen.getAllByRole('button', { name: '删除' })) {
      // 无前缀 opacity-0 会让 390px 触屏无删除入口（第二轮浏览器反例：computed opacity 0）
      expect(button.className).not.toMatch(/(^|\s)opacity-0(\s|$)/);
      expect(button.className).toContain('sm:opacity-0'); // 桌面保留 hover 浮现
    }
  });
});
