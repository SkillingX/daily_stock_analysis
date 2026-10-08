/**
 * Analysis-related type definitions.
 * Aligned with the API schema.
 */

// ============ Request Types ============

export type StockReportType = 'simple' | 'detailed' | 'full' | 'brief';
export type ReportType = StockReportType | 'market_review';
export type AnalysisPhase = 'auto' | 'premarket' | 'intraday' | 'postmarket';

export interface AnalysisRequest {
  stockCode?: string;
  stockCodes?: string[];
  reportType?: StockReportType;
  forceRefresh?: boolean;
  asyncMode?: boolean;
  analysisPhase?: AnalysisPhase;
  stockName?: string;
  originalQuery?: string;
  selectionSource?: 'manual' | 'autocomplete' | 'import' | 'image';
  notify?: boolean;
  skills?: string[];
  reportLanguage?: ReportLanguage;
}

export interface MarketReviewRequest {
  sendNotification?: boolean;
  reportLanguage?: ReportLanguage;
}

export interface MarketReviewAccepted {
  status: 'accepted';
  message: string;
  sendNotification: boolean;
  traceId?: string;
  taskId?: string;
}

// ============ Report Types ============

export type ReportLanguage = 'zh' | 'en';

export type MarketPhaseValue =
  | 'premarket'
  | 'intraday'
  | 'lunch_break'
  | 'closing_auction'
  | 'postmarket'
  | 'non_trading'
  | 'unknown';

export interface MarketPhaseSummary {
  market?: string | null;
  phase: MarketPhaseValue;
  marketLocalTime?: string | null;
  sessionDate?: string | null;
  effectiveDailyBarDate?: string | null;
  isTradingDay?: boolean | null;
  isMarketOpenNow?: boolean | null;
  isPartialBar?: boolean | null;
  minutesToOpen?: number | null;
  minutesToClose?: number | null;
  triggerSource?: string | null;
  analysisIntent?: string | null;
  warnings: string[];
}

/** Report metadata */
export interface ReportMeta {
  id?: number;  // Analysis history record ID, present for persisted reports
  queryId: string;
  stockCode: string;
  stockName: string;
  reportType: ReportType;
  reportLanguage?: ReportLanguage;
  createdAt: string;
  currentPrice?: number;
  changePct?: number;
  modelUsed?: string;  // Display-only model snapshot from persisted history; not used for runtime model selection
  marketPhaseSummary?: MarketPhaseSummary | null;
}

/** Sentiment label */
export type SentimentLabel =
  | '极度悲观'
  | '悲观'
  | '中性'
  | '乐观'
  | '极度乐观'
  | 'Very Bearish'
  | 'Bearish'
  | 'Neutral'
  | 'Bullish'
  | 'Very Bullish';

export type DecisionAction = 'buy' | 'add' | 'hold' | 'reduce' | 'sell' | 'watch' | 'avoid' | 'alert';

/** Report summary section */
export interface ReportSummary {
  analysisSummary: string;
  operationAdvice: string;
  action?: DecisionAction | null;
  actionLabel?: string | null;
  trendPrediction: string;
  sentimentScore: number;
  sentimentLabel?: SentimentLabel;
  /** 操作建议与趋势预测的原因 */
  actionReason?: string;
}

/** Strategy section */
export interface ReportStrategy {
  idealBuy?: string;
  secondaryBuy?: string;
  stopLoss?: string;
  takeProfit?: string;
}

export interface RelatedBoard {
  name: string;
  code?: string;
  type?: string;
}

export interface SectorRankingItem {
  name: string;
  changePct?: number;
}

export interface SectorRankings {
  top?: SectorRankingItem[];
  bottom?: SectorRankingItem[];
}

export interface MarketReviewPayloadSection {
  key?: string;
  title: string;
  markdown: string;
}

export interface MarketReviewIndex {
  code: string;
  name: string;
  current?: number;
  change?: number;
  changePct?: number;
  open?: number;
  high?: number;
  low?: number;
  volume?: number;
  amount?: number;
  amplitude?: number;
}

export interface MarketReviewBreadth {
  upCount?: number;
  downCount?: number;
  flatCount?: number;
  limitUpCount?: number;
  limitDownCount?: number;
  totalAmount?: number;
  turnoverUnit?: string;
}

export interface MarketReviewPayload {
  version?: number;
  kind?: 'market_review' | string;
  region?: string;
  language?: ReportLanguage | string;
  title?: string;
  rootTitle?: string;
  generatedAt?: string;
  date?: string;
  marketScope?: string;
  marketLight?: Record<string, unknown>;
  breadth?: MarketReviewBreadth;
  indices?: MarketReviewIndex[];
  sectors?: SectorRankings;
  news?: Array<Record<string, unknown>>;
  sections?: MarketReviewPayloadSection[];
  markets?: Record<string, MarketReviewPayload>;
  markdownReport?: string;
}

export type AnalysisContextPackBlockStatus =
  | 'available'
  | 'missing'
  | 'not_supported'
  | 'fallback'
  | 'stale'
  | 'estimated'
  | 'partial'
  | 'fetch_failed';

export interface AnalysisContextPackOverviewSubject {
  code: string;
  stockName?: string | null;
  market?: string | null;
}

export interface AnalysisContextPackOverviewBlock {
  key: string;
  label: string;
  status: AnalysisContextPackBlockStatus;
  source?: string | null;
  warnings: string[];
  missingReasons: string[];
  financialQuality?: Record<string, {
    status: 'missing' | 'unverified' | 'single_source' | 'not_comparable' | 'verified' | 'conflict';
    ruleEligible: boolean;
    source?: string | null;
    period?: string | null;
    periodBasis?: string | null;
    caliber?: string | null;
    unit?: string | null;
    currency?: string | null;
  }>;
}

export interface AnalysisContextPackOverviewCounts {
  available: number;
  missing: number;
  notSupported: number;
  fallback: number;
  stale: number;
  estimated: number;
  partial: number;
  fetchFailed: number;
}

export interface AnalysisContextPackOverviewMetadata {
  triggerSource?: string | null;
  newsResultCount?: number | null;
}

export type AnalysisContextPackDataQualityLevel = 'good' | 'usable' | 'limited' | 'poor';

export interface AnalysisContextPackOverviewDataQuality {
  overallScore?: number | null;
  level?: AnalysisContextPackDataQualityLevel | null;
  blockScores: Record<string, number>;
  limitations: string[];
}

export interface AnalysisContextPackOverview {
  packVersion: string;
  createdAt?: string | null;
  subject: AnalysisContextPackOverviewSubject;
  blocks: AnalysisContextPackOverviewBlock[];
  counts: AnalysisContextPackOverviewCounts;
  dataQuality?: AnalysisContextPackOverviewDataQuality | null;
  warnings: string[];
  metadata: AnalysisContextPackOverviewMetadata;
}

/** Details section */
export interface ReportDetails {
  newsContent?: string;
  rawResult?: Record<string, unknown>;
  contextSnapshot?: Record<string, unknown> & { marketReviewPayload?: MarketReviewPayload };
  analysisContextPackOverview?: AnalysisContextPackOverview | null;
  financialReport?: Record<string, unknown>;
  dividendMetrics?: Record<string, unknown>;
  belongBoards?: RelatedBoard[];
  sectorRankings?: SectorRankings;
}

/** Full analysis report */
export interface AnalysisReport {
  meta: ReportMeta;
  summary: ReportSummary;
  strategy?: ReportStrategy;
  details?: ReportDetails;
  /** 长线投研框架 - 六维评分 & 贝叶斯分析 */
  researchFramework?: ResearchFrameworkData;
  /** 贝叶斯框架 - ④贝叶斯评分表 */
  bayesianFramework?: BayesianFramework;
  /** 产业链解读 - ②产业链解读 */
  supplyChain?: SupplyChain;
  /** 长期价值与情景 - ③长期价值与情景 */
  valueScenarios?: ValueScenarios;
  /** 投资结论 - ①投资结论 */
  investmentConclusion?: InvestmentConclusion;
  /** 产业链分析（兼容旧字段） */
  supplyChainAnalysis?: SupplyChainAnalysis;
  /** 价值情景分析（兼容旧字段） */
  valueAnalysis?: ValueAnalysis;
}

// ============ Research Framework Types ============

export interface DimensionScore {
  dimension: string;
  weight: number;
  score: number;
  indicators?: Array<{
    name: string;
    score: number;
    weight: number;
    basis: string;
    summary?: string;
  }>;
}

export interface BayesianResult {
  priorP: number;
  marketImpliedP: number;
  edge: number;
  posteriorP: number;
  positionSuggestion: string;
  stopConditions?: {
    shouldStop: boolean;
    posteriorBelowPriorThreshold: boolean;
    strongNegativeEvidence: boolean;
    edgeDisappeared: boolean;
  };
}

export interface ResearchFrameworkData {
  dimensionTotal: number;
  dimensions: DimensionScore[];
  version: string;
  bayesianResult?: BayesianResult;
}

// ============ Agent Analysis Types ============

export interface ValueScenario {
  probability: number;
  valueAnchor: string;
  upsidePct?: number;
  downsidePct?: number;
  keyAssumptions?: string[];
  timeframeYears?: number;
}

export interface ValueAnalysis {
  valueHorizons?: ValueHorizons;
  scenarios?: {
    bullCase?: ValueScenario;
    baseCase?: ValueScenario;
    bearCase?: ValueScenario;
  };
  catalysts?: string[];
  risks?: string[];
  valueScore?: number;
}

export interface SupplyChainAnalysis {
  chainPosition?: string;
  chainPositionRationale?: string;
  moatType?: string;
  moatStrength?: string;
  moatRationale?: string;
  usChinaRisk?: string;
  chokepointType?: string;
  overallSupplyChainScore?: number;
  keyInsights?: string[];
  risks?: string[];
}

// ============ 五段式长线投研报告类型 ============

/** 贝叶斯框架 - ④贝叶斯评分表 */
export interface BayesianFramework {
  priorP: number;
  marketImpliedP: number;
  edge: number;
  posteriorP: number;
  positionSuggestion: string;
  evidenceLog?: EvidenceItem[];
  stopConditions?: StopConditions;
}

export interface EvidenceItem {
  evidence: string;
  strength: 'strong_pos' | 'weak_pos' | 'neutral' | 'weak_neg' | 'strong_neg';
  lr: number;
  posteriorP: number;
  date: string;
}

export interface StopConditions {
  shouldStop?: boolean;
  posteriorBelowPriorThreshold?: boolean;
  strongNegativeEvidence?: boolean;
  edgeDisappeared?: boolean;
}

/** 产业链解读 - ②产业链解读 */
export interface SupplyChain {
  chainMap?: ChainNode[];
  chokepoints?: Chokepoint[];
  companyPosition?: string;
  upstreamSuppliers?: string[];
  downstreamCustomers?: string[];
  usChinaChain?: USChinaChain;
  industryDrivers?: string[];
}

export interface ChainNode {
  level: string;
  companies?: string[];
  concentration?: string;
}

export interface Chokepoint {
  type: string;
  description: string;
  confidence?: 'high' | 'medium' | 'low';
}

export interface USChinaChain {
  role?: string;
  substitutionProgress?: string;
  sanctionRisk?: string;
  dualChainImpact?: string;
}

/** 长期价值与情景 - ③长期价值与情景 */
export interface ValueScenarios {
  industrySpace?: string;
  competitiveEvolution?: string;
  scenarios?: Scenario[];
  horizons?: ValueHorizons;
  catalysts?: string[];
  risks?: string[];
}

export interface Scenario {
  type: 'optimistic' | 'neutral' | 'pessimistic';
  probability: number;
  valueAnchor?: number | string;
  description?: string;
  upsidePct?: number;
  downsidePct?: number;
  keyAssumptions?: string[];
}

export interface ValueHorizons {
  horizon1y?: string;
  horizon3y?: string;
  horizon5y?: string;
}

/** 投资结论 - ①投资结论 */
export interface InvestmentConclusion {
  priorP?: number;
  marketImpliedP?: number;
  edge?: number;
  posteriorP?: number;
  position?: string;
  action?: '建仓' | '加仓' | '持有' | '减仓' | '止损' | '观察';
  chainPositionSummary?: string;
  valueRange1y?: string;
  valueRange3y?: string;
  valueRange5y?: string;
  rationale?: string;
  stopConditions?: StopConditions;
  buildPositionRecords?: PositionRecord[];
}

export interface PositionRecord {
  date: string;
  action: string;
  price: string;
  positionPct: number;
  priorP: number;
  edge: number;
}

// ============ Analysis Result Types ============

export type RunDiagnosticStatus = 'normal' | 'degraded' | 'failed' | 'unknown';

export type RunDiagnosticComponentStatus =
  | 'ok'
  | 'degraded'
  | 'failed'
  | 'unknown'
  | 'not_configured'
  | 'skipped';

export interface RunDiagnosticComponent {
  key: string;
  label: string;
  status: RunDiagnosticComponentStatus;
  message: string;
  details?: Record<string, unknown>;
}

export interface RunDiagnosticSummary {
  traceId?: string;
  taskId?: string;
  queryId?: string;
  stockCode?: string;
  triggerSource?: string;
  status: RunDiagnosticStatus;
  statusLabel: string;
  reason: string;
  components: Record<string, RunDiagnosticComponent>;
  copyText: string;
}

/** Sync analysis response */
export interface AnalysisResult {
  queryId: string;
  traceId?: string;
  stockCode: string;
  stockName: string;
  report: AnalysisReport;
  diagnosticSummary?: RunDiagnosticSummary;
  createdAt: string;
}

/** Async task accepted response */
export interface TaskAccepted {
  taskId: string;
  traceId?: string;
  status: 'pending' | 'processing';
  message?: string;
  analysisPhase?: AnalysisPhase;
}

export interface BatchTaskAcceptedItem {
  taskId: string;
  traceId?: string;
  stockCode: string;
  status: 'pending' | 'processing';
  message?: string;
  analysisPhase?: AnalysisPhase;
}

export interface BatchDuplicateTaskItem {
  stockCode: string;
  existingTaskId: string;
  message: string;
}

export interface BatchTaskAcceptedResponse {
  accepted: BatchTaskAcceptedItem[];
  duplicates: BatchDuplicateTaskItem[];
  message: string;
}

export type AnalyzeAsyncResponse = TaskAccepted | BatchTaskAcceptedResponse;

export type AnalyzeResponse = AnalysisResult | AnalyzeAsyncResponse;

/** Task status */
export interface TaskStatus {
  taskId: string;
  traceId?: string;
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'cancel_requested' | 'cancelled';
  progress?: number;
  result?: AnalysisResult;
  marketReviewReport?: string;
  marketReviewPayload?: MarketReviewPayload;
  error?: string;
  stockName?: string;
  originalQuery?: string;
  selectionSource?: string;
  analysisPhase?: AnalysisPhase | null;
  skills?: string[];
}

/** Task details used by task list and SSE events */
export interface TaskInfo {
  taskId: string;
  traceId?: string;
  stockCode: string;
  stockName?: string;
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'cancel_requested' | 'cancelled';
  progress: number;
  message?: string;
  reportType: string;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
  error?: string;
  originalQuery?: string;
  selectionSource?: string;
  analysisPhase?: AnalysisPhase;
  skills?: string[];
}

/** Task list response */
export interface TaskListResponse {
  total: number;
  pending: number;
  processing: number;
  tasks: TaskInfo[];
}

/** Duplicate task error response */
export interface DuplicateTaskError {
  error: 'duplicate_task';
  message: string;
  stockCode: string;
  existingTaskId: string;
}

// ============ History Types ============

/** History item summary */
export interface HistoryItem {
  id: number;  // Record primary key ID, always present for persisted history items
  queryId: string;  // Linked analysis query ID
  stockCode: string;
  stockName?: string;
  reportType?: ReportType;
  trendPrediction?: string;
  analysisSummary?: string;
  sentimentScore?: number;
  operationAdvice?: string;
  action?: DecisionAction | null;
  actionLabel?: string | null;
  currentPrice?: number;
  changePct?: number;
  volumeRatio?: number;
  turnoverRate?: number;
  modelUsed?: string;  // Display-only model snapshot from persisted history; runtime provider/model/base URL still come from analyzer configuration
  marketPhaseSummary?: MarketPhaseSummary | null;
  createdAt: string;
}

export type StockHistoryRange = 'all' | '30d' | '90d';

export interface StockHistoryFilters {
  range: StockHistoryRange;
  model: string;
  sort: 'desc' | 'asc';
}

/** History list response */
export interface HistoryListResponse {
  total: number;
  page: number;
  limit: number;
  items: HistoryItem[];
}

/** News item */
export interface NewsIntelItem {
  title: string;
  snippet: string;
  url: string;
}

/** News response */
export interface NewsIntelResponse {
  total: number;
  items: NewsIntelItem[];
}

/** History filter parameters */
export interface HistoryFilters {
  stockCode?: string;
  reportType?: ReportType;
  startDate?: string;
  endDate?: string;
}

/** History pagination parameters */
export interface HistoryPagination {
  page: number;
  limit: number;
}

// ============ Stock Bar Types ============

export interface StockBarItem {
  id: number;
  stockCode: string;
  stockName?: string;
  reportType?: string;
  sentimentScore?: number;
  operationAdvice?: string;
  action?: DecisionAction | null;
  actionLabel?: string | null;
  analysisCount: number;
  lastAnalysisTime?: string;
  modelUsed?: string;
  marketPhaseSummary?: MarketPhaseSummary | null;
}

export interface StockBarResponse {
  total: number;
  items: StockBarItem[];
}

// ============ Error Types ============

export interface ApiError {
  error: string;
  message: string;
  detail?: Record<string, unknown>;
}

// ============ Helper Functions ============

/** Get sentiment label by score */
export const getSentimentLabel = (score: number, language: ReportLanguage = 'zh'): SentimentLabel => {
  if (language === 'en') {
    if (score <= 20) return 'Very Bearish';
    if (score <= 40) return 'Bearish';
    if (score <= 60) return 'Neutral';
    if (score <= 80) return 'Bullish';
    return 'Very Bullish';
  }
  if (score <= 20) return '极度悲观';
  if (score <= 40) return '悲观';
  if (score <= 60) return '中性';
  if (score <= 80) return '乐观';
  return '极度乐观';
};

/** Get sentiment color by score */
export const getSentimentColor = (score: number): string => {
  if (score <= 20) return '#ef4444'; // red-500
  if (score <= 40) return '#f97316'; // orange-500
  if (score <= 60) return '#eab308'; // yellow-500
  if (score <= 80) return '#22c55e'; // green-500
  return '#10b981'; // emerald-500
};
