import { useNavigate } from 'react-router-dom';
import { ArrowRight, FileSearch } from 'lucide-react';
import type { AnalysisReport } from '../../types/analysis';
import { cn } from '../../utils/cn';

/**
 * 每日分析结果页的投研摘要卡（决策 #4：摘要卡 + 跳页，子报告链接随行）。
 *
 * 数据来自长线五段字段（A 股 = 双轨引擎 v2 产出，港美/回退 = 内嵌框架）；
 * 无 researchFramework 的旧记录不渲染。完整结论与 13 份子报告经跳页到深度投研页查看。
 */
export function LongtrackSummaryCard({ report }: { report: AnalysisReport }) {
  const navigate = useNavigate();
  const framework = report.researchFramework;
  const conclusion = report.investmentConclusion;
  if (!framework || typeof framework.dimensionTotal !== 'number') {
    return null;
  }

  const code = report.meta.stockCode;
  const name = report.meta.stockName || '';
  const action = conclusion?.action;
  const total = framework.dimensionTotal;
  const scoreTone =
    total >= 65 ? 'text-emerald-400' : total >= 50 ? 'text-amber-300' : 'text-red-400';

  return (
    <div className="mb-3 rounded-xl border border-cyan/25 bg-cyan/5 px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex items-center gap-2">
          <FileSearch className="h-4 w-4 text-cyan" aria-hidden="true" />
          <span className="text-xs text-muted-text">投研总评分</span>
          <span className={cn('text-xl font-bold', scoreTone)}>
            {total.toFixed(1)}
            <span className="text-xs font-normal text-muted-text">/100</span>
          </span>
          {framework.version && (
            <span className="rounded bg-white/8 px-1.5 py-0.5 text-[10px] text-muted-text">
              {framework.version}
            </span>
          )}
        </div>
        {action && (
          <span className="rounded bg-white/8 px-2 py-0.5 text-sm text-secondary-text">
            长线行动：<strong className="text-foreground">{action}</strong>
          </span>
        )}
        {conclusion?.edge != null && (
          <span className="text-xs text-muted-text">
            认知差 {conclusion.edge > 0 ? '+' : ''}
            {conclusion.edge.toFixed(2)}
          </span>
        )}
        <button
          type="button"
          onClick={() =>
            navigate(`/deep-research?code=${encodeURIComponent(code)}&name=${encodeURIComponent(name)}`)
          }
          className="ml-auto inline-flex items-center gap-1 rounded-lg bg-cyan px-3 py-1.5 text-xs font-semibold text-black transition-colors hover:bg-cyan/90"
        >
          查看完整深度报告（含 13 份子报告）
          <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>
      {framework.dimensions?.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {framework.dimensions.map((d) => (
            <span
              key={d.dimension}
              className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] text-muted-text"
              title={`权重 ${(d.weight * 100).toFixed(0)}%`}
            >
              {d.dimension} {d.score.toFixed(0)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
