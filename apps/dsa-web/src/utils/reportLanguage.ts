import type { ReportLanguage } from '../types/analysis';

export const normalizeReportLanguage = (value?: string | null): ReportLanguage =>
  value === 'en' ? 'en' : 'zh';

const REPORT_TEXT = {
  zh: {
    keyInsights: '核心洞察',
    noAnalysisSummary: '暂无分析结论',
    actionAdvice: '操作建议',
    noAdvice: '暂无建议',
    trendPrediction: '趋势预测',
    noPrediction: '暂无预测',
    marketSentiment: '市场情绪',
    strategyPoints: '策略点位',
    sniperLevels: '狙击点位',
    idealBuy: '理想买入',
    secondaryBuy: '二次买入',
    stopLoss: '止损价位',
    takeProfit: '止盈目标',
    noValue: '—',
    newsFeed: '资讯动态',
    relatedNews: '相关资讯',
    refresh: '刷新',
    retry: '重试',
    dismiss: '关闭',
    details: '查看详情',
    loadingNews: '加载资讯中...',
    noNews: '暂无相关资讯',
    noNewsDescription: '可稍后刷新以获取最新资讯。',
    openLink: '跳转',
    transparency: '透明度',
    traceability: '数据追溯',
    rawResult: '原始分析结果',
    analysisSnapshot: '分析快照',
    copy: '复制',
    copied: '已复制',
    recordId: '记录 ID',
    fullReport: '完整分析报告',
    loadingReport: '加载报告中...',
    loadReportFailed: '加载报告失败',
    copyMarkdownSource: '复制 Markdown 源码',
    copyPlainText: '复制纯文本',
    analysisModel: '分析模型',
    fearGreedIndex: '恐惧贪婪指数',
    boardLinkage: '板块联动',
    relatedBoards: '关联板块',
    leadingBoard: '领涨',
    laggingBoard: '领跌',
    neutralBoard: '中性',
    reanalyze: '重新分析',
    noRelatedBoards: '暂无板块联动信息',
    priorPH: 'P(H)（假设成立的先验概率）',
    edge: 'Edge',
    longTermPosition: '长线仓位',
    aiPerspective: 'AI观点',
    marketImplied: '市场预期',

    // === ⑤ 六维详情（Six-Dimension Detail Panel）===
    sixDimensionTitle: '⑤ 六维详情',
    noSixDimensionData: '暂无六维详情数据',
    weightPrefix: '权重',
    indicatorSplitLabel: '细分指标',
    confidenceSuffix: '可信度',
    reliabilityLabel: '可信度',
    reliabilityLow: '低 · 多数维度回退至中性分',
    reliabilityNormal: '正常',
    scoreLegend: '图例：',
    scoreStrong: '强势',
    scoreNeutral: '中性',
    scoreWeak: '弱势',
    scoreAriaLabel: '总分',
    weightShort: '权重',
    // Dimension sub-captions (locale-aware, displayed beneath the dimension title)
    dimCaptionSupplyChain: '上下游链路与卡点评估',
    dimCaptionFundamental: '估值、盈利与成长性',
    dimCaptionCapital: '资金流向与机构持仓变动',
    dimCaptionTechnical: '趋势、量能、波动',
    dimCaptionSentiment: '分析师共识与新闻情绪',
    dimCaptionMacro: '货币与地缘宏观环境',

    // === Scoring enums (basis / chain / moat / sentiment / macro...) ===
    basisRule: '规则',
    basisLlm: 'LLM',
    basisDataProvider: '数据',
    basisRuleShort: 'R',
    basisLlmShort: 'L',
    basisDataProviderShort: 'D',
  },
  en: {
    keyInsights: 'KEY INSIGHTS',
    noAnalysisSummary: 'No analysis summary yet',
    actionAdvice: 'Action Advice',
    noAdvice: 'No advice yet',
    trendPrediction: 'Trend Outlook',
    noPrediction: 'No forecast yet',
    marketSentiment: 'Market Sentiment',
    strategyPoints: 'STRATEGY POINTS',
    sniperLevels: 'Action Levels',
    idealBuy: 'Ideal Entry',
    secondaryBuy: 'Secondary Entry',
    stopLoss: 'Stop Loss',
    takeProfit: 'Take Profit',
    noValue: '—',
    newsFeed: 'NEWS FEED',
    relatedNews: 'Related News',
    refresh: 'Refresh',
    retry: 'Retry',
    dismiss: 'Close',
    details: 'View details',
    loadingNews: 'Loading news...',
    noNews: 'No related news',
    noNewsDescription: 'Refresh later to check for the latest updates.',
    openLink: 'Open',
    transparency: 'TRANSPARENCY',
    traceability: 'Data Traceability',
    rawResult: 'Raw Analysis Result',
    analysisSnapshot: 'Analysis Snapshot',
    copy: 'Copy',
    copied: 'Copied!',
    recordId: 'Record ID',
    fullReport: 'Full Analysis Report',
    loadingReport: 'Loading report...',
    loadReportFailed: 'Failed to load report',
    copyMarkdownSource: 'Copy Markdown Source',
    copyPlainText: 'Copy Plain Text',
    analysisModel: 'Model',
    fearGreedIndex: 'Fear & Greed Index',
    boardLinkage: 'BOARD LINKAGE',
    relatedBoards: 'Related Boards',
    leadingBoard: 'Leading',
    laggingBoard: 'Lagging',
    neutralBoard: 'Neutral',
    reanalyze: 'Reanalyze',
    noRelatedBoards: 'No board linkage info',
    priorPH: 'P(H)(Prior Probability of Hypothesis)',
    edge: 'Edge',
    longTermPosition: 'Long-term Position',
    aiPerspective: 'AI View',
    marketImplied: 'Market Implied',

    // === ⑤ 六维详情（Six-Dimension Detail Panel）===
    sixDimensionTitle: '⑤ Six-Dimension Detail',
    noSixDimensionData: 'No six-dimension detail available',
    weightPrefix: 'Weight',
    indicatorSplitLabel: 'Sub-indicators',
    confidenceSuffix: 'confidence',
    reliabilityLabel: 'Reliability',
    reliabilityLow: 'Low — most dimensions fell back to neutral',
    reliabilityNormal: 'Normal',
    scoreLegend: 'Legend:',
    scoreStrong: 'Strong',
    scoreNeutral: 'Neutral',
    scoreWeak: 'Weak',
    scoreAriaLabel: 'Total score',
    weightShort: 'W',
    dimCaptionSupplyChain: 'Upstream/downstream positioning & chokepoints',
    dimCaptionFundamental: 'Valuation, profitability & growth',
    dimCaptionCapital: 'Capital flow & institutional holdings',
    dimCaptionTechnical: 'Trend, volume & volatility',
    dimCaptionSentiment: 'Analyst consensus & news sentiment',
    dimCaptionMacro: 'Monetary policy & geopolitical backdrop',

    // === Scoring enums (basis / chain / moat / sentiment / macro...) ===
    basisRule: 'Rule',
    basisLlm: 'LLM',
    basisDataProvider: 'Data',
    basisRuleShort: 'R',
    basisLlmShort: 'L',
    basisDataProviderShort: 'D',
  },
} as const;

export const getReportText = (language?: string | null) => REPORT_TEXT[normalizeReportLanguage(language)];

// ────────────────────────────────────────────────────────────────────
// Six-dimension panel: localization dictionaries for enum values
// emitted by the scoring service. The backend writes raw enum
// strings (e.g. "bullish", "midstream") that may appear mixed with
// Chinese sentence templates in `summary`. The frontend rewrites
// them so the panel reads as one coherent language.
// ────────────────────────────────────────────────────────────────────

export type SixDimensionEnumKey =
  // basis
  | 'rule' | 'llm' | 'data_provider'
  // ma_alignment
  | 'bullish' | 'bearish' | 'ma_neutral'
  // trend status (sentiment / pullback / rally / falling)
  | 'rising' | 'falling' | 'oscillating'
  // volume_trend
  | 'increasing' | 'stable' | 'decreasing'
  // regulatory_risk
  | 'risk_low' | 'risk_medium' | 'risk_high'
  // chain_position
  | 'upstream' | 'bottleneck' | 'midstream' | 'downstream' | 'commodity'
  // moat_type / chokepoint_type
  | 'patent' | 'technology' | 'brand' | 'network' | 'switching_cost' | 'license' | 'regulatory'
  | 'tech' | 'capacity' | 'geo' | 'cert'
  // moat_strength
  | 'strong' | 'moderate' | 'weak' | 'none'
  // cognitive_difference
  | 'market_underestimating' | 'market_fair' | 'market_overestimating'
  // analyst_consensus
  | 'buy' | 'outperform' | 'underperform' | 'sell'
  // news_sentiment
  | 'positive' | 'neutral' | 'negative'
  // macro
  | 'accommodative' | 'tight' | 'minimal' | 'limited' | 'significant' | 'severe'
  | 'abundant' | 'liquidity_moderate' | 'scarce'
  | 'moat_strength_moderate'
  | 'supportive' | 'restrictive';

export const SIX_DIMENSION_ENUM_LABELS: { [lang in ReportLanguage]: { [K in SixDimensionEnumKey]: string } } = {
  zh: {
    rule: '规则', llm: 'LLM', data_provider: '数据',
    bullish: '多头', bearish: '空头', ma_neutral: '中性走势',
    rising: '上升', falling: '下降', oscillating: '震荡',
    increasing: '放大', stable: '平稳', decreasing: '缩小',
    risk_high: '高', risk_medium: '中', risk_low: '低',
    upstream: '上游', bottleneck: '卡点', midstream: '中游', downstream: '下游', commodity: '大宗',
    patent: '专利', technology: '技术', brand: '品牌', network: '网络效应',
    switching_cost: '切换成本', license: '牌照', regulatory: '牌照壁垒',
    tech: '技术', capacity: '产能', geo: '地缘', cert: '认证',
    strong: '强', moat_strength_moderate: '中等', moderate: '中', weak: '弱', none: '无',
    market_underestimating: '市场低估', market_fair: '估值合理', market_overestimating: '市场高估',
    buy: '买入', outperform: '增持', underperform: '减持', sell: '卖出',
    positive: '正面', neutral: '中性', negative: '负面',
    accommodative: '宽松', tight: '紧缩',
    abundant: '宽松', liquidity_moderate: '适度', scarce: '紧张',
    supportive: '支持', restrictive: '限制',
    minimal: '极小', limited: '有限', significant: '显著', severe: '严重',
  },
  en: {
    rule: 'Rule', llm: 'LLM', data_provider: 'Data',
    bullish: 'Bullish', bearish: 'Bearish', ma_neutral: 'Neutral trend',
    rising: 'Rising', falling: 'Falling', oscillating: 'Oscillating',
    increasing: 'Up', stable: 'Stable', decreasing: 'Down',
    risk_high: 'High', risk_medium: 'Medium', risk_low: 'Low',
    upstream: 'Upstream', bottleneck: 'Bottleneck', midstream: 'Midstream', downstream: 'Downstream', commodity: 'Commodity',
    patent: 'Patent', technology: 'Technology', brand: 'Brand', network: 'Network',
    switching_cost: 'Switching Cost', license: 'License', regulatory: 'Regulatory',
    tech: 'Tech', capacity: 'Capacity', geo: 'Geo', cert: 'Cert',
    strong: 'Strong', moat_strength_moderate: 'Average', moderate: 'Moderate', weak: 'Weak', none: 'None',
    market_underestimating: 'Undervalued by Market', market_fair: 'Fairly Valued', market_overestimating: 'Overvalued by Market',
    buy: 'Buy', outperform: 'Outperform', underperform: 'Underperform', sell: 'Sell',
    positive: 'Positive', neutral: 'Neutral', negative: 'Negative',
    accommodative: 'Accommodative', tight: 'Tight',
    abundant: 'Abundant', liquidity_moderate: 'Moderate', scarce: 'Scarce',
    supportive: 'Supportive', restrictive: 'Restrictive',
    minimal: 'Minimal', limited: 'Limited', significant: 'Significant', severe: 'Severe',
  },
};

/**
 * Translate a backend enum string. Falls back to the raw string when not in
 * the dictionary — preserves any value the backend may add later.
 */
export const translateSixDimensionEnum = (
  value: string | undefined | null,
  language?: string | null,
): string => {
  if (!value) return '';
  const labels = SIX_DIMENSION_ENUM_LABELS[normalizeReportLanguage(language)];
  const key = value.toLowerCase();
  // Try the canonical lookup first; for ambiguous tokens
  // (high/medium/low) try the risk_ variants.
  let translated: string | undefined = (labels as Record<string, string>)[key];
  if (!translated && (key === 'high' || key === 'low' || key === 'medium')) {
    translated = (labels as Record<string, string>)[`risk_${key}`];
  }
  return translated ?? value;
};

/**
 * Localize the dimension key the backend writes (e.g. "产业链定位").
 * Returns the canonical Chinese when zh, otherwise the English label
 * in the dimensionLabels map. Falls back to the original string when
 * no entry matches.
 */
export const SIX_DIMENSION_LOCALES: Record<string, { zh: string; en: string }> = {
  '产业链定位': { zh: '产业链定位', en: 'Supply Chain' },
  '基本面与价值': { zh: '基本面与价值', en: 'Fundamentals' },
  '资金面': { zh: '资金面', en: 'Capital Flow' },
  '技术面': { zh: '技术面', en: 'Technical' },
  '情绪与认知差': { zh: '情绪与认知差', en: 'Sentiment & Edge' },
  '宏观与地缘': { zh: '宏观与地缘', en: 'Macro & Geopolitics' },
};

export const localizeDimensionName = (
  rawKey: string | undefined | null,
  language?: string | null,
): string => {
  if (!rawKey) return '';
  const lang = normalizeReportLanguage(language);
  const entry = SIX_DIMENSION_LOCALES[rawKey];
  if (entry) return entry[lang];
  // Try English lookup (in case backend already sent an English key)
  for (const { en, zh } of Object.values(SIX_DIMENSION_LOCALES)) {
    if (en === rawKey) return zh;  // always keep canonical Chinese when zh
    if (en === rawKey) return en;  // no-op for English
    if (zh === rawKey) return lang === 'en' ? en : zh;
  }
  // Reverse lookup by en name
  for (const [zhKey, { en, zh }] of Object.entries(SIX_DIMENSION_LOCALES)) {
    if (en === rawKey) return lang === 'en' ? en : zh;
    if (zhKey === rawKey) return lang === 'en' ? en : zhKey;
  }
  return rawKey;
};

/**
 * Rewrite a scoring enum summary (`评级:underperform, 目标价空间-10.0%`)
 * so every leaf token is translated into the active language. Word-boundary
 * safe: matches `key:value` patterns and bare tokens.
 */
export const localizeEnumInSummary = (
  summary: string | undefined | null,
  language?: string | null,
): string => {
  if (!summary) return '';
  const lang = normalizeReportLanguage(language);
  const dict = SIX_DIMENSION_ENUM_LABELS[lang];

  const lookup = (token: string): string | undefined => {
    const key = token.toLowerCase();
    let translated: string | undefined = (dict as Record<string, string>)[key];
    if (!translated && key === 'neutral') {
      translated = (dict as Record<string, string>)['ma_neutral'];
    }
    if (!translated && (key === 'high' || key === 'low' || key === 'medium')) {
      translated = (dict as Record<string, string>)[`risk_${key}`];
    }
    return translated;
  };

  // Match tokens after `key:` (e.g. "评级:underperform") and bare tokens.
  return summary.replace(
    /([A-Za-z_]+)(:[ \t]*)([A-Za-z][A-Za-z_]+)/g,
    (full, _key, sep, value: string) => {
      const translated = lookup(value);
      return translated ? `${sep}${translated}` : full;
    },
  ).replace(
    /\b(upstream|downstream|midstream|bottleneck|commodity|bullish|bearish|neutral|rising|falling|oscillating|increasing|stable|decreasing|patent|technology|brand|network|switching_cost|license|regulatory|tech|capacity|geo|cert|strong|moderate|weak|none|market_underestimating|market_fair|market_overestimating|buy|outperform|underperform|sell|positive|negative|accommodative|tight|abundant|scarce|supportive|restrictive|minimal|limited|significant|severe|high|medium|low|data_provider|rule|llm)\b/g,
    (token: string) => lookup(token) ?? token,
  );
};
