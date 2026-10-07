import { useCallback, useEffect, useState } from 'react';
import { Download, FileText, LayoutGrid, Loader2, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { ReportMarkdownBody } from '../components/report/ReportMarkdownBody';
import { StockAutocomplete } from '../components/StockAutocomplete/StockAutocomplete';
import apiClient from '../api';

interface SectorItem {
  id: string;
  stock_code: string;
  stock_name?: string;
  created_at?: string;
  sector_score?: number | null;
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) || '';

export default function SectorAnalysisPage() {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<{ code: string; name?: string } | null>(null);
  const [history, setHistory] = useState<SectorItem[]>([]);
  const [detail, setDetail] = useState<{ id: string; markdown: string; stock_name?: string } | null>(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadHistory = useCallback(async () => {
    const res = await apiClient.get<{ data: SectorItem[] }>('/api/v1/sector-analysis/reports');
    setHistory(res.data.data || []);
  }, []);

  useEffect(() => { void loadHistory(); }, [loadHistory]);

  const handleGenerate = useCallback(async () => {
    if (!selected) { setError('请先选择一只 A 股'); return; }
    setError(null); setGenerating(true); setDetail(null);
    try {
      const res = await apiClient.post<{ report_id: string | null; markdown: string }>(
        `/api/v1/sector-analysis/generate?stock_code=${encodeURIComponent(selected.code)}&stock_name=${encodeURIComponent(selected.name || '')}`,
      );
      if (res.data.report_id) {
        setDetail({ id: res.data.report_id, markdown: res.data.markdown });
        await loadHistory();
      } else {
        setError('报告落库失败，请重试');
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  }, [selected, loadHistory]);

  const handleSelect = useCallback(async (id: string) => {
    const res = await apiClient.get<{ data: { markdown: string; stock_name?: string } }>(
      `/api/v1/sector-analysis/reports/${id}`,
    );
    setDetail({ id, markdown: res.data.data.markdown, stock_name: res.data.data.stock_name });
  }, []);

  const handleDelete = useCallback(async (id: string) => {
    await apiClient.delete(`/api/v1/sector-analysis/reports/${id}`);
    await loadHistory();
    if (detail?.id === id) setDetail(null);
  }, [loadHistory, detail]);

  return (
    <div className="mx-auto flex min-h-[calc(100vh-5rem)] w-full max-w-6xl gap-4 px-4">
      <aside className="sticky top-4 hidden w-64 flex-shrink-0 self-start rounded-[1.25rem] border border-white/8 bg-card/82 p-3 md:block">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-[0.2em] text-cyan">历史报告</h2>
          <button onClick={() => void loadHistory()} aria-label="刷新"><RefreshCw className="h-4 w-4" /></button>
        </div>
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
            <button className="opacity-0 group-hover:opacity-100" onClick={() => void handleDelete(h.id)} aria-label="删除">
              <Trash2 className="h-3.5 w-3.5 text-muted-text hover:text-red-400" />
            </button>
          </div>
        ))}
      </aside>

      <main className="min-w-0 flex-1">
        <header className="mb-4 space-y-3">
          <div className="flex items-center gap-2">
            <LayoutGrid className="h-6 w-6 text-cyan" />
            <h1 className="text-2xl font-bold">个股板块分析</h1>
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
              {generating ? '生成中（约30秒）...' : '生成报告'}
            </button>
          </div>
          {error && <p className="text-sm text-red-400">{error}</p>}
        </header>

        <div className="rounded-[1.25rem] border border-white/8 bg-card/82 p-5">
          {generating && (
            <div className="flex items-center gap-3 py-16 text-secondary-text">
              <Loader2 className="h-5 w-5 animate-spin text-cyan" />
              <span>板块识别/政策倾向/行业基率/板块景气分析中，请勿关闭页面...</span>
            </div>
          )}
          {!generating && !detail && (
            <div className="py-16 text-center text-muted-text">
              <LayoutGrid className="mx-auto mb-3 h-10 w-10 opacity-40" />
              <p>输入股票，生成板块定位/政策倾向（行业DNA库）/行业基率（可迭代）/板块景气的专项分析报告</p>
            </div>
          )}
          {detail && !generating && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <a
                  className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5 text-sm text-secondary-text hover:bg-white/5"
                  href={`${API_BASE}/api/v1/sector-analysis/reports/${detail.id}/markdown?download=1`}
                  download
                >
                  <Download className="h-4 w-4" /> 下载 Markdown
                </a>
                <a
                  className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5 text-sm text-secondary-text hover:bg-white/5"
                  href={`${API_BASE}/api/v1/sector-analysis/reports/${detail.id}/markdown`}
                  target="_blank"
                  rel="noreferrer"
                >
                  <FileText className="h-4 w-4" /> 查看原文件
                </a>
              </div>
              <ReportMarkdownBody content={detail.markdown} className="deep-research-prose" />
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
