// 报告速览首屏（issue #103 工单 01）：行业层级（一级排名+二级目录映射）、
// 5/20/60 日三序列趋势（存档数据本地归一与切换，不重新取数）、近七天催化摘要。
// 全部数据来自报告存档载荷（analysis / analysis_json），旧报告（无新增键）不渲染。
import { useState } from 'react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

function asNumber(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function asString(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null;
}

interface TrendSeries {
  role: string;
  name: string;
  index_code: string;
  source: string;
  expected_sessions: number;
  qualified_sessions: number;
  observations: { date: string; close: number }[];
  period_return_pct: number | null;
  gaps: string[];
}

interface TrendWindow {
  window_days: number;
  start_date: string | null;
  end_date: string | null;
  series: TrendSeries[];
  gaps: string[];
}

// issue #104：二级同业块（成交活跃代表 + 全成分成交额名次），来自存档载荷。
interface L2PeerItem {
  code: string;
  name: string;
  amount: number | null;
  change_pct: number | null;
  is_target: boolean;
  as_of_date: string | null;
}

interface L2PeersBlock {
  items: L2PeerItem[];
  target_amount_rank: number | null;
  amount_rank_denominator: number | null;
  amount_rank_scope: string | null;
  amount_rank_evidence: string | null;
  target_change_pct_rank: number | null;
  sample_size: number;
  snapshot_rows: number;
  rankable_rows: number;
  // issue #104 复审追加（旧存档无键 → null，不假定未采集的已知零数）：
  valid_quote_rows: number | null;
  change_pct_qualified_rows: number | null;
  verified_constituent_num: number | null;
  quotes_as_of: string | null;
  industry_reference: boolean;
  target_independent_quote: L2PeerItem | null;
  gaps: string[];
}

function parseL2PeerItem(v: unknown): L2PeerItem | null {
  const rec = asRecord(v);
  if (!rec) return null;
  const code = asString(rec.code);
  if (!code) return null;
  return {
    code,
    name: asString(rec.name) ?? code,
    amount: asNumber(rec.amount),
    change_pct: asNumber(rec.change_pct),
    is_target: rec.is_target === true,
    as_of_date: asString(rec.as_of_date),
  };
}

function parseL2Peers(v: unknown): L2PeersBlock | null {
  const rec = asRecord(v);
  if (!rec) return null;
  const items: L2PeerItem[] = Array.isArray(rec.items)
    ? rec.items.map(parseL2PeerItem).filter((i): i is L2PeerItem => i !== null)
    : [];
  const gaps = Array.isArray(rec.gaps)
    ? rec.gaps.filter((g): g is string => typeof g === 'string')
    : [];
  const scope = asString(rec.amount_rank_scope);
  return {
    items,
    target_amount_rank: asNumber(rec.target_amount_rank),
    amount_rank_denominator: asNumber(rec.amount_rank_denominator),
    amount_rank_scope:
      scope === 'full_constituents' || scope === 'qualified_sample'
        ? scope
        : null,
    amount_rank_evidence: asString(rec.amount_rank_evidence),
    target_change_pct_rank: asNumber(rec.target_change_pct_rank),
    sample_size: asNumber(rec.sample_size) ?? 0,
    snapshot_rows: asNumber(rec.snapshot_rows) ?? 0,
    rankable_rows: asNumber(rec.rankable_rows) ?? 0,
    valid_quote_rows: asNumber(rec.valid_quote_rows),
    change_pct_qualified_rows: asNumber(rec.change_pct_qualified_rows),
    verified_constituent_num: asNumber(rec.verified_constituent_num),
    quotes_as_of: asString(rec.quotes_as_of),
    industry_reference: rec.industry_reference === true,
    target_independent_quote: parseL2PeerItem(rec.target_independent_quote),
    gaps,
  };
}

function formatAmount(v: number): string {
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)} 亿`;
  if (v >= 1e4) return `${(v / 1e4).toFixed(0)} 万`;
  // 正但不足 1 元（含可表示的极小值）：保留原值字面，与真零 0 有清楚
  // 区别——服务端仍按正额排行，页面不得把它显示成「0」（issue #104 六轮）
  if (v > 0 && v < 1) return String(v);
  return v.toFixed(0);
}

const ROLE_LABEL: Record<string, string> = {
  l2_industry: '二级',
  l1_industry: '一级',
  benchmark: '沪深300',
};

const ROLE_COLOR: Record<string, string> = {
  l2_industry: '#22d3ee',
  l1_industry: '#f59e0b',
  benchmark: '#94a3b8',
};

function parseSeries(v: unknown): TrendSeries | null {
  const rec = asRecord(v);
  if (!rec) return null;
  const role = asString(rec.role);
  const name = asString(rec.name);
  const code = asString(rec.index_code);
  if (!role || !name || !code) return null;
  const observations: { date: string; close: number }[] = [];
  if (Array.isArray(rec.observations)) {
    for (const o of rec.observations) {
      const row = asRecord(o);
      const date = row ? asString(row.date) : null;
      const close = row ? asNumber(row.close) : null;
      if (date && close !== null && close > 0) observations.push({ date, close });
    }
  }
  return {
    role,
    name,
    index_code: code,
    source: asString(rec.source) ?? 'iFinD 标准 HTTP 指数日线',
    expected_sessions: asNumber(rec.expected_sessions) ?? 0,
    qualified_sessions: asNumber(rec.qualified_sessions) ?? 0,
    observations,
    period_return_pct: asNumber(rec.period_return_pct),
    gaps: Array.isArray(rec.gaps)
      ? rec.gaps.filter((g): g is string => typeof g === 'string')
      : [],
  };
}

function parseWindows(v: unknown): TrendWindow[] {
  if (!Array.isArray(v)) return [];
  const windows: TrendWindow[] = [];
  for (const w of v) {
    const rec = asRecord(w);
    const days = rec ? asNumber(rec.window_days) : null;
    if (!rec || days === null) continue;
    windows.push({
      window_days: days,
      start_date: asString(rec.start_date),
      end_date: asString(rec.end_date),
      series: Array.isArray(rec.series)
        ? rec.series.map(parseSeries).filter((s): s is TrendSeries => s !== null)
        : [],
      gaps: Array.isArray(rec.gaps)
        ? rec.gaps.filter((g): g is string => typeof g === 'string')
        : [],
    });
  }
  return windows;
}

/** 三条序列从同窗首个收盘日各自归一为 100。唯一可画判定源（一次遍历，
 * Line/图例/画线缺口说明都复用本结果，不对同一候选重复判定）：
 * - 全窗合格序列可画；收益被判定不可发布（收益留空且该序列带缺口）时
 *   曲线留空；窗口期望不足（合格数=期望数且无序列缺口）保留既有曲线；
 * - 归一化结果非有限或非正（窗口观测收盘极值组合上溢 Infinity / 小比值
 *   下溢 0 被当成真实点）的序列不画——数据与 Line/图例一致隔离，隔离
 *   序列进 hiddenRoles 供画线缺口说明复用。 */
function buildChartData(
  series: TrendSeries[],
): { data: Record<string, number | string>[];
    drawableRoles: Set<string>;
    hiddenRoles: Set<string> } {
  const byDate = new Map<string, Record<string, number | string>>();
  const drawableRoles = new Set<string>();
  const hiddenRoles = new Set<string>();
  for (const s of series) {
    if (s.qualified_sessions !== s.expected_sessions || !s.observations.length) {
      continue;
    }
    if (s.period_return_pct === null && s.gaps.length > 0) {
      continue; // 收益不可发布（如区间收益超出可表示范围）→ 曲线留空
    }
    const base = s.observations[0].close;
    if (!(base > 0)) continue;
    const normalized: { date: string; value: number }[] = [];
    let drawable = true;
    for (const o of s.observations) {
      let v = (o.close / base) * 100;
      // 正收盘比值的中间除法下溢为 0（如 MIN_VALUE/3）：先按 100 缩放再除
      // 的等价计算可表示时保留该点（issue #103 五轮）；等价式仍非有限/非正
      // 才是真不可表示（Infinity 与归一化零保持禁画）
      if (v <= 0 && o.close > 0 && base > 0) {
        v = (o.close * 100) / base;
      }
      if (!Number.isFinite(v) || v <= 0) {
        drawable = false;
        break;
      }
      normalized.push({ date: o.date, value: v });
    }
    if (!drawable) {
      hiddenRoles.add(s.role);
      continue;
    }
    drawableRoles.add(s.role);
    for (const { date, value } of normalized) {
      const row = byDate.get(date) ?? { date: date.slice(5) };
      row[s.role] = value;
      byDate.set(date, row);
    }
  }
  return { data: [...byDate.values()], drawableRoles, hiddenRoles };
}

function pct(v: number): string {
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
}

function CatalystBar({ catalysts }: { catalysts: Record<string, unknown> | null }) {
  const items = catalysts && Array.isArray(catalysts.items) ? catalysts.items : [];
  const first = asRecord(items[0]);
  // 部分检索失败数由后端结构化摘要发布（issue #105 修复：已找到催化时
  // 并列标注部分检索渠道失败，不遮住失败事实；旧存档无该键则不标注）。
  const summary = catalysts ? asRecord(catalysts.first_screen) : null;
  const partialFailures = summary && typeof summary.partial_search_failures === 'number'
    && Number.isFinite(summary.partial_search_failures)
    && summary.partial_search_failures > 0
    ? summary.partial_search_failures
    : null;
  if (first) {
    const title = asString(first.title) ?? '';
    const url = asString(first.url) ?? '#';
    const date = asString(first.published_date) ?? '日期未知';
    const domain = asString(first.source_domain) ?? '';
    return (
      <p className="text-sm text-secondary-text">
        近七天催化（首条）：
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-cyan hover:underline"
        >
          {title}
        </a>
        <span className="text-muted-text">
          {' '}
          （{date} 发布{domain ? `，${domain}` : ''}）
        </span>
        {partialFailures !== null && (
          // 两主题可读警示（issue #105 修复二轮）：amber-400 在浅色背景
          // 对比度不足；按项目内 dark: 变体惯例取浅色 amber-700（浅底
          // 4.69:1，过 WCAG AA）/深色 amber-300（深底 13.29:1）
          <span className="text-xs text-amber-700 dark:text-amber-300">
            {' '}⚠ 部分检索渠道失败 {partialFailures} 个（已取得催化照常展示，诊断见行业催化节）
          </span>
        )}
      </p>
    );
  }
  // 三态由后端结构化摘要发布（不解析 gap 文案：worker 诊断与聚合诊断的
  // gaps 顺序不代表检索结论），首条缺失时按旧存档回退到资格文案。
  const state = summary ? asString(summary.state) : null;
  const STATE_TEXT: Record<string, string> = {
    none_found: '未发现合格催化（检索成功、经统一准入无近期催化）',
    search_failed: '全部检索失败（不冒充「窗口内无新闻」）',
    partial_failed: '部分检索渠道失败且无合格催化',
    search_unavailable: '搜索入口未配置，本次未搜索',
    budget_exhausted: '取数预算不足，搜索未完成',
  };
  if (state && STATE_TEXT[state]) {
    return <p className="text-sm text-muted-text">近七天催化：{STATE_TEXT[state]}</p>;
  }
  const eligible = catalysts?.eligible === true;
  return (
    <p className="text-sm text-muted-text">
      近七天催化：{eligible ? '未发现合格催化' : '催化检索未执行（主行业身份未确认）'}
    </p>
  );
}

// issue #104：二级同业表（同一存档载荷，生成/历史回看一致；旧存档无键不渲染）。
// 名次行标注范围（全成分需 iFinD 双接口核验一致，否则样本范围）与分母；
// 覆盖统计暴露观察成分数与有效报价数；缺口逐条透出。
function L2PeersTable({ peers, chainGaps, l2Label }: {
  peers: L2PeersBlock | null;
  chainGaps: string[];
  l2Label: string | null;
}) {
  if (!peers && chainGaps.length === 0) return null;
  const hasTarget = peers?.items.some((i) => i.is_target) ?? false;
  const scopeText = peers?.amount_rank_scope === 'full_constituents'
    ? '全成分正额范围'
    : peers?.amount_rank_scope === 'qualified_sample'
      ? '有效正额样本范围（全成分门槛未满足）'
      : null;
  return (
    <div className="space-y-1.5" aria-label="二级同业对照">
      {peers ? (
        <>
          <p className="text-sm font-medium text-secondary-text">
            二级同业{peers.industry_reference ? '（行业参考）' : ''}
            {l2Label ? ` · ${l2Label} 成分口径` : ''}
          </p>
          {peers.industry_reference && (
            <p className="text-xs text-amber-400/90">
              ⚠ 目标股未确认在二级指数成分内，以下为行业参考、不构成与目标股同成分的可比样本
            </p>
          )}
          {peers.target_independent_quote && (
            <p className="text-sm text-secondary-text">
              目标股独立行情：
              {peers.target_independent_quote.code}（{peers.target_independent_quote.name}）
              ｜ 涨跌幅 {peers.target_independent_quote.change_pct !== null
                ? pct(peers.target_independent_quote.change_pct) : 'N/A'}
              ｜ 成交额 {peers.target_independent_quote.amount !== null
                ? `${formatAmount(peers.target_independent_quote.amount)} 元` : 'N/A'}
              {peers.target_independent_quote.as_of_date
                ? ` ｜ 行情日期 ${peers.target_independent_quote.as_of_date}` : ''}
            </p>
          )}
          {peers.items.length > 0 && (
            <table className="w-full text-left text-xs">
              <thead className="text-muted-text">
                <tr>
                  <th className="py-1 pr-3 font-medium">代码</th>
                  <th className="py-1 pr-3 font-medium">名称</th>
                  <th className="py-1 pr-3 font-medium">成交额（元）</th>
                  <th className="py-1 pr-3 font-medium">涨跌幅</th>
                  <th className="py-1 font-medium">目标股</th>
                </tr>
              </thead>
              <tbody>
                {peers.items.map((p) => (
                  <tr key={p.code} className="border-t border-white/5">
                    <td className="py-1 pr-3">{p.code}</td>
                    <td className="py-1 pr-3">{p.name}</td>
                    <td className="py-1 pr-3">
                      {p.amount !== null ? formatAmount(p.amount) : 'N/A'}
                    </td>
                    <td className="py-1 pr-3">
                      {p.change_pct !== null ? (
                        <span className={p.change_pct >= 0
                          ? 'text-red-400' : 'text-emerald-400'}>
                          {pct(p.change_pct)}
                        </span>
                      ) : 'N/A'}
                    </td>
                    <td className="py-1">{p.is_target ? '✔' : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="text-xs text-muted-text">
            排名范围与覆盖：观察成分 {peers.snapshot_rows} 行
            {/* issue #104 复审：行情覆盖（正额+已知零）与正额名次分母分开；
                旧存档无 valid_quote_rows 时不假定未采集的已知零数 */}
            {peers.valid_quote_rows !== null
              ? ` · 有效报价 ${peers.valid_quote_rows} 行（含已知零 ${
                  peers.valid_quote_rows - peers.rankable_rows}，正额可排名 ${peers.rankable_rows}）`
              : ` · 有效报价 ${peers.rankable_rows} 行（正额口径，未采集已知零数）`}
            {peers.amount_rank_denominator !== null
              ? ` · 正额名次分母 ${peers.amount_rank_denominator}` : ''}
            {' '}· 样本 {peers.sample_size} 家
            {peers.change_pct_qualified_rows !== null
              ? ` · 有效涨跌幅覆盖 ${peers.change_pct_qualified_rows}/${peers.sample_size}` : ''}
            {peers.quotes_as_of ? ` · 行情时点 ${peers.quotes_as_of}` : ''}
          </p>
          {peers.verified_constituent_num !== null && (
            <p className="text-xs text-muted-text">
              已验证成分数：{peers.verified_constituent_num}
              （iFinD 双接口核验一致；核验事实独立于全量名次是否发布，适用缺口见下方）
            </p>
          )}
          {peers.target_amount_rank !== null && scopeText && (
            <p className="text-sm text-secondary-text">
              目标股成交额名次：第 {peers.target_amount_rank} /{' '}
              {peers.amount_rank_denominator ?? '—'}（{scopeText}；降序顺序位，同额按代码先后）
            </p>
          )}
          {peers.amount_rank_evidence && (
            <p className="text-xs text-muted-text">
              全成分核验：{peers.amount_rank_evidence}
            </p>
          )}
          {peers.target_change_pct_rank !== null && hasTarget && (
            <p className="text-sm text-secondary-text">
              目标股涨跌幅相对位置：样本内第 {peers.target_change_pct_rank} /{' '}
              {peers.sample_size}（并列同名次）
            </p>
          )}
          {peers.gaps.map((g, i) => (
            <p key={i} className="text-xs text-amber-400/90">⚠ {g}</p>
          ))}
        </>
      ) : (
        chainGaps.length > 0 && (
          <p className="text-xs text-amber-400/90">
            ⚠ 二级同业未取得：{chainGaps[0]}
          </p>
        )
      )}
    </div>
  );
}

export default function SectorFirstScreen({
  analysis,
}: {
  analysis: Record<string, unknown> | null | undefined;
}) {
  const [activeWindow, setActiveWindow] = useState(20);
  const root = asRecord(analysis);
  const l2Identity = root ? asRecord(root.sector_l2_identity) : null;
  const windows = root ? parseWindows(root.sector_trend_windows) : [];
  const windowGaps = root && Array.isArray(root.sector_trend_windows_gaps)
    ? (root.sector_trend_windows_gaps.filter(
        (g) => typeof g === 'string') as string[])
    : [];
  const current = windows.find((w) => w.window_days === activeWindow) ?? windows[0];
  // 窗口数据 ≤ 61×3 点，直接重算（React Compiler 自动 memo 化，不手动 useMemo）；
  // 可画判定只有这一份（Line/图例与画线缺口说明都复用同一次构图结果）
  const chart = current
    ? buildChartData(current.series)
    : { data: [] as Record<string, number | string>[],
        drawableRoles: new Set<string>(), hiddenRoles: new Set<string>() };
  const { data: chartData, drawableRoles, hiddenRoles } = chart;
  // 被归一化边界隔离的序列（一次构图中已判定）：解释画线缺口
  const hiddenCurves = current
    ? current.series.filter((s) => hiddenRoles.has(s.role))
    : [];
  // 旧报告（无新增键）不渲染首屏，保持原 Markdown 呈现
  if (!l2Identity && windows.length === 0 && windowGaps.length === 0) return null;
  const intraday = root ? asRecord(asRecord(root.background)?.intraday) : null;
  const row = intraday ? asRecord(intraday.row) : null;
  const identity = root ? asRecord(root.identity) : null;
  const l1Name = asString(identity?.board_name) ?? '未锁定';
  const l1Code = asString(identity?.board_code);
  const changePct = row ? asNumber(row.change_pct) : null;
  const rankOf = intraday ? asNumber(intraday.rank_of) : null;
  const totalBoards = intraday ? asNumber(intraday.total_boards) : null;
  const snapshotDate = intraday ? asString(intraday.snapshot_date) : null;
  const snapshotMode = asString(intraday?.snapshot_mode);
  const isReference = snapshotMode === 'reference';
  const isPreviousClose = snapshotMode === 'previous_close';
  const dayLabel = isReference ? '参考日' : isPreviousClose ? '收盘日' : '当日';
  const dataSource = current?.series[0]?.source
    ?? 'iFinD 标准 HTTP 指数日线';

  return (
    <section
      aria-label="报告速览"
      className="mb-4 space-y-3 rounded-2xl border border-white/10 bg-white/[0.03] p-4"
    >
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        {/* 主题前景色（issue #105 修复）：text-primary-text 未在 Tailwind
            主题中定义，深色主题下继承近黑前景导致对比度约 1.13:1；改用
            主题前景令牌，两种主题可读 */}
        <h2 className="text-base font-semibold text-foreground">报告速览</h2>
        <span className="text-sm text-secondary-text">
          {l1Name}
          {l1Code ? `（${l1Code}）` : ''}
          {changePct !== null && (
            <span className={changePct >= 0 ? 'text-red-400' : 'text-emerald-400'}>
              {' '}{dayLabel}涨跌幅 {pct(changePct)}
            </span>
          )}
        </span>
        {rankOf !== null && totalBoards !== null && (
          <span className="text-xs text-muted-text">
            {dayLabel}排名 {rankOf} / {totalBoards}（31 个申万一级行业）
          </span>
        )}
        {snapshotDate && (
          <span className="text-xs text-muted-text">
            {isReference ? '参考口径' : isPreviousClose ? '收盘日期' : '行情日期'} {snapshotDate}
            {isReference ? '（目标交易日合格行情未取得，展示最近已完成交易日）' : ''}
          </span>
        )}
      </div>
      {l2Identity && (
        <p className="text-sm text-secondary-text">
          {l2Identity.status === 'confirmed' ? (
            <>
              二级对照：<span className="text-foreground">{asString(l2Identity.l2_name)}（{asString(l2Identity.l2_index_code)}）</span>
              {' '}· 父行业：{asString(l2Identity.l1_parent_name)}
              {l2Identity.observation_date ? ` · 归属观察日 ${asString(l2Identity.observation_date)}` : ''}
              {/* 目录版本与核验口径随已确认身份带出（issue #103 三轮：
                  速览节跳过后目录版本在页面仍可见；旧存档无该键则省略） */}
              {asString(l2Identity.directory_version)
                ? ` · 目录版本 ${asString(l2Identity.directory_version)}（官方发布目录 + iFinD 核验子集，缺失/歧义不映射）`
                : ''}
            </>
          ) : (
            <span className="text-muted-text">
              二级对照未采纳
              {Array.isArray(l2Identity.gaps) && l2Identity.gaps.length
                ? `：${String(l2Identity.gaps[0])}`
                : ''}
            </span>
          )}
        </p>
      )}
      {windows.length > 0 && current && (
        <div className="space-y-2">
          <div className="flex items-center gap-2" role="tablist" aria-label="趋势窗口">
            {[5, 20, 60].map((d) => (
              <button
                key={d}
                role="tab"
                aria-selected={(current.window_days === d)}
                onClick={() => setActiveWindow(d)}
                className={`rounded-lg border px-2.5 py-1 text-xs ${
                  current.window_days === d
                    ? 'border-cyan bg-cyan/10 text-cyan'
                    : 'border-white/10 text-secondary-text hover:bg-white/5'
                }`}
              >
                {d}日
              </button>
            ))}
            <span className="text-xs text-muted-text">
              {current.start_date} → {current.end_date}（存档数据本地切换，不重新取数）
            </span>
            <span className="text-xs text-muted-text">数据来源：{dataSource}</span>
          </div>
          {chartData.length > 0 ? (
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke="#ffffff14" />
                  <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#64748b' }} />
                  <YAxis
                    domain={['auto', 'auto']}
                    tick={{ fontSize: 11, fill: '#64748b' }}
                    width={52}
                  />
                  <Tooltip />
                  <Legend formatter={(v: unknown) => ROLE_LABEL[String(v)] ?? String(v)} />
                  <ReferenceLine y={100} stroke="#ffffff30" strokeDasharray="4 4" />
                  {current.series
                    .filter((s) => drawableRoles.has(s.role))
                    .map((s) => (
                      <Line
                        key={s.role}
                        type="monotone"
                        dataKey={s.role}
                        name={s.name}
                        stroke={ROLE_COLOR[s.role] ?? '#22d3ee'}
                        dot={false}
                        strokeWidth={1.8}
                      />
                    ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="text-sm text-muted-text">当前窗口无合格序列曲线（见下方缺口）</p>
          )}
          {/* 画线缺口说明（issue #103 三轮）：候选可画但被归一化边界隔离的
              序列——收益与合格观测保留，只隔离异常曲线/图例 */}
          {hiddenCurves.map((s) => (
            <p key={`hidden-${s.role}`} className="text-xs text-amber-400/90">
              ⚠ {s.name}曲线未绘制：窗口观测收盘极值组合使归一化值不可表示（上溢或
              下溢为 0），该序列窗口收益与合格观测保留
            </p>
          ))}
          <table className="w-full text-left text-xs">
            <thead className="text-muted-text">
              <tr>
                <th className="py-1 pr-3 font-medium">序列</th>
                <th className="py-1 pr-3 font-medium">窗口收益</th>
                <th className="py-1 font-medium">合格观测</th>
              </tr>
            </thead>
            <tbody>
              {current.series.map((s) => (
                <tr key={s.role} className="border-t border-white/5">
                  <td className="py-1 pr-3">
                    <span
                      className="mr-1.5 inline-block h-2 w-2 rounded-full align-middle"
                      style={{ backgroundColor: ROLE_COLOR[s.role] ?? '#22d3ee' }}
                    />
                    {ROLE_LABEL[s.role] ?? s.role} · {s.name}（{s.index_code}）
                  </td>
                  <td className="py-1 pr-3">
                    {s.period_return_pct !== null ? (
                      <span className={s.period_return_pct >= 0 ? 'text-red-400' : 'text-emerald-400'}>
                        {pct(s.period_return_pct)}
                      </span>
                    ) : (
                      <span className="text-muted-text">留空（缺口见下）</span>
                    )}
                  </td>
                  <td className="py-1">
                    {s.qualified_sessions}/{s.expected_sessions}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {[...current.gaps,
            ...current.series.flatMap((s) => s.gaps.map((g) => `${s.name}：${g}`))].map(
            (g, i) => (
              <p key={i} className="text-xs text-amber-400/90">⚠ {g}</p>
            ),
          )}
          {/* 块级缺口（issue #103 复审）：有窗口时也随趋势块展示一次，
              不逐窗口重复；全无窗口的展示见下方互斥分支 */}
          {windowGaps.length > 0 && windowGaps.map((g, i) => (
            <p key={`blk-${i}`} className="text-xs text-amber-400/90">
              ⚠ 多窗口趋势缺口：{g}
            </p>
          ))}
        </div>
      )}
      {windows.length === 0 && windowGaps.length > 0 && (
        <p className="text-xs text-amber-400/90">
          ⚠ 5/20/60 日多窗口趋势未取得：{windowGaps[0]}
        </p>
      )}
      <L2PeersTable peers={root ? parseL2Peers(root.sector_l2_peers) : null}
        chainGaps={root && Array.isArray(root.sector_l2_peers_gaps)
          ? (root.sector_l2_peers_gaps.filter(
              (g) => typeof g === 'string') as string[])
          : []}
        l2Label={l2Identity?.status === 'confirmed'
          ? `${asString(l2Identity.l2_name)}（${asString(l2Identity.l2_index_code)}）`
          : null} />
      <CatalystBar catalysts={asRecord(root?.sector_catalysts)} />
    </section>
  );
}
