import { useCallback, useEffect, useState } from 'react';
import { Download, FileText, History, LayoutGrid, Loader2, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { ReportMarkdownBody } from '../components/report/ReportMarkdownBody';
import { StockAutocomplete } from '../components/StockAutocomplete/StockAutocomplete';
import apiClient from '../api';
import { parseApiError, type ParsedApiError } from '../api/error';

interface SectorItem {
  id: string;
  stock_code: string;
  stock_name?: string;
  created_at?: string;
  sector_score?: number | null;
}

interface GenerateResult {
  report_id: string | null;
  stock_code: string;
  stock_name?: string | null;
  markdown: string;
  analysis: { payload_version?: number } & Record<string, unknown>;
}

interface DeleteResult {
  success: boolean;
  deleted: string;
  file_cleanup?: 'ok' | 'failed';
  message?: string | null;
}

/** 生成请求等待上限：后端报告总预算 300s + 收尾/传输余量（issue #79 实施默认）。 */
const GENERATE_WAIT_TIMEOUT_MS = 330000;

/** 从 axios 错误中提取服务端 message/detail（后端错误信封为 {error, message, detail}）。 */
function extractServerMessage(err: unknown): string | null {
  if (err && typeof err === 'object' && 'response' in err) {
    const data = (err as { response?: { data?: unknown } }).response?.data;
    if (data && typeof data === 'object' && 'message' in data) {
      const msg = (data as { message?: unknown }).message;
      if (typeof msg === 'string' && msg.trim()) return msg;
    }
    if (typeof data === 'object' && data !== null && 'detail' in data) {
      const detail = (data as { detail?: unknown }).detail;
      if (typeof detail === 'string' && detail.trim()) return detail;
    }
  }
  return null;
}

/** blob 响应的错误体：服务端错误信封在 Blob 里，需本页局部解析后才能展示
 *  （否则只会显示「下载失败：{}」）。解析失败时至少给出 HTTP 状态。 */
async function describeBlobError(err: unknown, fallback: string): Promise<string> {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as { response?: { status?: number; data?: unknown } }).response;
    if (resp?.data instanceof Blob) {
      let server: string | null = null;
      try {
        const parsed = JSON.parse(await resp.data.text()) as {
          message?: unknown;
          detail?: unknown;
        };
        for (const candidate of [parsed.message, parsed.detail]) {
          if (typeof candidate === 'string' && candidate.trim()) {
            server = candidate.trim();
            break;
          }
        }
      } catch {
        // 非 JSON 错误体：回退 HTTP 状态
      }
      const status = typeof resp.status === 'number' ? `（HTTP ${resp.status}）` : '';
      return `${fallback}：${server ?? '服务端返回错误'}${status}`;
    }
  }
  return describeError(err, fallback);
}

/** 用户可见错误文案：优先服务端原文，客户端等待超时单独给历史回看提示。 */
function describeError(err: unknown, fallback: string): string {
  const server = extractServerMessage(err);
  if (server) return `${fallback}：${server}`;
  const parsed: ParsedApiError = parseApiError(err);
  if (parsed.category === 'upstream_timeout') {
    return `${fallback}：前端等待超时（${Math.round(GENERATE_WAIT_TIMEOUT_MS / 1000)} 秒上限）。生成可能仍在后台进行，稍后可刷新历史查看，请勿视为已成功生成。`;
  }
  return `${fallback}：${parsed.message}`;
}

export default function SectorAnalysisPage() {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<{ code: string; name?: string } | null>(null);
  const [history, setHistory] = useState<SectorItem[]>([]);
  const [detail, setDetail] = useState<{ id: string; markdown: string; stock_name?: string } | null>(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mobileHistoryOpen, setMobileHistoryOpen] = useState(false);

  const loadHistory = useCallback(async () => {
    try {
      const res = await apiClient.get<{ data: SectorItem[] }>(`/api/v1/sector-analysis/reports`);
      setHistory(res.data.data || []);
    } catch (e: unknown) {
      setError(describeError(e, '历史报告加载失败'));
    }
  }, []);

  useEffect(() => { void loadHistory(); }, [loadHistory]);

  const handleGenerate = useCallback(async () => {
    if (!selected) { setError('请先选择一只 A 股'); return; }
    if (generating) { return; } // 防重复提交：生成中忽略再次点击
    setError(null); setGenerating(true); setDetail(null);
    try {
      const res = await apiClient.post<GenerateResult>(
        `/api/v1/sector-analysis/generate?stock_code=${encodeURIComponent(selected.code)}&stock_name=${encodeURIComponent(selected.name || '')}`,
        undefined,
        { timeout: GENERATE_WAIT_TIMEOUT_MS },
      );
      if (res.data.report_id) {
        setDetail({ id: res.data.report_id, markdown: res.data.markdown, stock_name: res.data.stock_name || undefined });
        await loadHistory();
      } else {
        setError('生成反馈异常：未返回报告 id，请勿视为已保存，可重试');
      }
    } catch (e: unknown) {
      setError(describeError(e, '生成失败'));
    } finally {
      setGenerating(false);
    }
  }, [selected, generating, loadHistory]);

  const handleSelect = useCallback(async (id: string) => {
    setError(null);
    try {
      const res = await apiClient.get<{ data: { markdown: string; stock_name?: string } }>(
        `/api/v1/sector-analysis/reports/${id}`,
      );
      setDetail({ id, markdown: res.data.data.markdown, stock_name: res.data.data.stock_name });
    } catch (e: unknown) {
      setError(describeError(e, '报告详情打开失败'));
    }
  }, []);

  const handleDelete = useCallback(async (id: string) => {
    setError(null);
    try {
      const res = await apiClient.delete<DeleteResult>(
        `/api/v1/sector-analysis/reports/${id}`,
      );
      await loadHistory();
      if (detail?.id === id) setDetail(null);
      // 部分失败如实可见：记录已删除但存档文件清理失败（后端 file_cleanup=failed）
      if (res.data?.file_cleanup === 'failed') {
        setError(res.data.message || '记录已删除，但存档文件清理失败');
      }
    } catch (e: unknown) {
      setError(describeError(e, '删除失败'));
    }
  }, [loadHistory, detail]);

  /** 下载/查看原文件：经统一 apiClient（同 API 基址与认证语义）取回，
   *  失败在页面可见——原生 <a> 链接的 500 只产生浏览器错误页，无页面反馈。 */
  const handleFetchFile = useCallback(async (id: string, mode: 'download' | 'view') => {
    setError(null);
    try {
      const res = await apiClient.get<Blob>(
        `/api/v1/sector-analysis/reports/${id}/markdown${mode === 'download' ? '?download=1' : ''}`,
        { responseType: 'blob', timeout: 30000 },
      );
      const url = URL.createObjectURL(res.data);
      if (mode === 'download') {
        const a = document.createElement('a');
        a.href = url;
        a.download = `${id}.md`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 60000);
      } else {
        window.open(url, '_blank', 'noopener');
        window.setTimeout(() => URL.revokeObjectURL(url), 60000);
      }
    } catch (e: unknown) {
      setError(await describeBlobError(e, mode === 'download' ? '下载失败' : '原文件打开失败'));
    }
  }, []);

  const historyList = (
    <>
      {history.length === 0 && <p className="p-2 text-xs text-muted-text">暂无报告</p>}
      {history.map((h) => (
        <div key={h.id} className="group mb-1 flex items-center rounded-lg p-2 hover:bg-white/5">
          <button className="min-w-0 flex-1 text-left" onClick={() => void handleSelect(h.id)}>
            <div className="truncate text-sm">{h.stock_name || h.stock_code}</div>
            <div className="text-xs text-muted-text">
              {h.stock_code}
              {h.sector_score != null && ` · ${h.sector_score}分`}
              {' · '}{(h.created_at || '').slice(0, 16).replace('T', ' ')}
            </div>
          </button>
          {/* 删除控件：触屏/小屏默认可见（不再依赖 hover），桌面 sm+ 保留悬停浮现；
              p-1 保证触控目标，aria-label 提供可理解名称。 */}
          <button
            className="group-hover:opacity-100 p-1 sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
            onClick={() => void handleDelete(h.id)}
            aria-label="删除"
          >
            <Trash2 className="h-3.5 w-3.5 text-muted-text hover:text-red-400" />
          </button>
        </div>
      ))}
    </>
  );

  return (
    <div className="mx-auto flex min-h-[calc(100vh-5rem)] w-full max-w-6xl gap-4 px-4">
      <aside className="sticky top-4 hidden w-64 flex-shrink-0 self-start rounded-[1.25rem] border border-white/8 bg-card/82 p-3 md:block">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-cyan">历史报告</h2>
          <button onClick={() => void loadHistory()} aria-label="刷新"><RefreshCw className="h-4 w-4" /></button>
        </div>
        {historyList}
      </aside>

      <main className="min-w-0 flex-1">
        <header className="mb-4 space-y-3">
          <div className="flex items-center gap-2">
            <LayoutGrid className="h-6 w-6 text-cyan" />
            <h1 className="text-2xl font-bold">个股板块分析</h1>
          </div>
          {/* 移动端历史入口：小屏幕以抽屉方式访问历史（桌面端保持侧栏） */}
          <div className="md:hidden">
            <button
              onClick={() => setMobileHistoryOpen((open) => !open)}
              aria-expanded={mobileHistoryOpen}
              className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5 text-sm text-secondary-text hover:bg-white/5"
            >
              <History className="h-4 w-4" /> 历史报告
            </button>
            {mobileHistoryOpen && (
              <div className="mt-2 rounded-xl border border-white/8 bg-card/82 p-2">
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-xs uppercase tracking-widest text-muted-text">历史报告</span>
                  <button onClick={() => void loadHistory()} aria-label="刷新历史"><RefreshCw className="h-3.5 w-3.5" /></button>
                </div>
                {historyList}
              </div>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="min-w-[240px] flex-1">
              <StockAutocomplete
                value={query}
                onChange={setQuery}
                onSubmit={(code, name) => { setSelected({ code, name }); setQuery(name ? `${name} ${code}` : code); setError(null); }}
                placeholder="输入 A 股代码或名称（如 603690 / 至纯科技）"
                disabled={generating}
              />
            </div>
            <button
              onClick={() => void handleGenerate()}
              disabled={generating}
              className="inline-flex h-11 items-center gap-2 rounded-xl bg-cyan px-5 text-sm font-semibold text-black hover:bg-cyan/90 disabled:opacity-60"
            >
              {generating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
              {generating ? '生成中（约30秒-5分钟）...' : '生成报告'}
            </button>
          </div>
          {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
        </header>

        <div className="rounded-[1.25rem] border border-white/8 bg-card/82 p-5">
          {generating && (
            <div className="flex items-center gap-3 py-16 text-secondary-text">
              <Loader2 className="h-5 w-5 animate-spin text-cyan" />
              <span>板块识别/当日行情/板块趋势/同业比较分析中，请勿关闭页面...</span>
            </div>
          )}
          {!generating && !detail && (
            <div className="py-16 text-center text-muted-text">
              <LayoutGrid className="mx-auto mb-3 h-10 w-10 opacity-40" />
              <p>输入股票，生成主行业定位（东财板块成分验证）/当日行情/近期板块趋势（20个已完成交易日，对比沪深300）/同业比较（成交活跃代表）/政策倾向（行业DNA库）/行业基率（启发式先验）的专项分析报告</p>
            </div>
          )}
          {detail && !generating && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <button
                  className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5 text-sm text-secondary-text hover:bg-white/5"
                  onClick={() => void handleFetchFile(detail.id, 'download')}
                >
                  <Download className="h-4 w-4" /> 下载 Markdown
                </button>
                <button
                  className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5 text-sm text-secondary-text hover:bg-white/5"
                  onClick={() => void handleFetchFile(detail.id, 'view')}
                >
                  <FileText className="h-4 w-4" /> 查看原文件
                </button>
              </div>
              <ReportMarkdownBody content={detail.markdown} className="deep-research-prose" />
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
