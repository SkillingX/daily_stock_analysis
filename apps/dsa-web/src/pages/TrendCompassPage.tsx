import { useState } from 'react';
import { AppPage, Badge, Button, Card, Collapsible, EmptyState, Loading, PageHeader } from '../components/common';
import { Tooltip } from '../components/common/Tooltip';
import { ReportMarkdownBody } from '../components/report/ReportMarkdownBody';
import { StockAutocomplete } from '../components/StockAutocomplete';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import { trendCompassApi } from '../api/trendCompass';
import type { CompassAction, CompassAnalyzeResult } from '../api/trendCompass';
import { cn } from '../utils/cn';

type Lang = 'zh' | 'en';

// Domain labels are contract enums (plan v2 §8), kept zh/en side by side (§13.6).
const ACTION_TEXT: Record<CompassAction, Record<Lang, string>> = {
  buy: { zh: '买入', en: 'Buy' },
  watch: { zh: '观望', en: 'Watch' },
  sell: { zh: '卖出', en: 'Sell' },
};

const ACTION_CLASS: Record<CompassAction, string> = {
  buy: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  watch: 'border-amber-500/50 bg-amber-500/10 text-amber-600 dark:text-amber-400',
  sell: 'border-rose-500/50 bg-rose-500/10 text-rose-600 dark:text-rose-400',
};

const LAYER_TEXT: Record<string, Record<Lang, string>> = {
  weekly_bull: { zh: '周线多头', en: 'Weekly bull' },
  weekly_bear: { zh: '周线空头', en: 'Weekly bear' },
  weekly_transition: { zh: '周线转换', en: 'Weekly transition' },
  weekly_disabled: { zh: '周线样本不足', en: 'Weekly disabled' },
  annual_bull: { zh: '年线多头', en: 'Annual bull' },
  annual_bear: { zh: '年线空头', en: 'Annual bear' },
  annual_transition: { zh: '年线转换', en: 'Annual transition' },
  annual_disabled: { zh: '年线样本不足', en: 'Annual disabled' },
  alive: { zh: '趋势段仍在', en: 'Alive' },
  resting: { zh: '休整（回踩/反抽）', en: 'Resting' },
  flattening: { zh: '收口', en: 'Flattening' },
  broken: { zh: '结构破坏', en: 'Broken' },
  healthy: { zh: '节奏健康', en: 'Healthy' },
  cooling: { zh: '节奏降温', en: 'Cooling' },
  exhausted: { zh: '节奏失配', en: 'Exhausted' },
  noisy: { zh: '数据嘈杂', en: 'Noisy' },
};

const PHASE_TEXT: Record<string, Record<Lang, string>> = {
  trend_expanding: { zh: '趋势扩张', en: 'Trend expanding' },
  trend_holding: { zh: '趋势持有', en: 'Trend holding' },
  trend_tiring: { zh: '趋势疲惫', en: 'Trend tiring' },
  coiling: { zh: '收敛蓄力', en: 'Coiling' },
  transitioning: { zh: '阶段切换', en: 'Transitioning' },
};

const BAR_STATUS_TEXT: Record<string, Record<Lang, string>> = {
  closed: { zh: '收盘确认', en: 'Closed bar' },
  intraday_unconfirmed: { zh: '盘中未确认', en: 'Intraday unconfirmed' },
  stale: { zh: '数据停滞', en: 'Stale' },
  suspended: { zh: '停牌', en: 'Suspended' },
};

const POSITION_TEXT: Record<string, Record<Lang, string>> = {
  full: { zh: '允许重仓波段', en: 'Full position allowed' },
  half: { zh: '半仓上限', en: 'Half position cap' },
  none: { zh: '禁止买入', en: 'No buying allowed' },
};

const HORIZON_TEXT: Record<string, Record<Lang, string>> = {
  '1w': { zh: '执行窗：未来 1 周', en: 'Execution window: 1 week' },
  '2w': { zh: '节奏窗：未来 2 周', en: 'Rhythm window: 2 weeks' },
  '1m': { zh: '主趋势窗：未来 1 个月', en: 'Segment window: 1 month' },
};

// Reason-code chain copy for the rewrite panel. Mirrors the backend i18n
// _REASON table (src/services/compass/i18n.py); kept here for instant
// rendering — keep the two semantically in sync.
const REASON_STEP_TEXT: Record<string, Record<Lang, { title: string; detail: string }>> = {
  data_missing: {
    zh: { title: '数据缺失或停滞', detail: '行情数据不可用，按最保守处理，降级为观望' },
    en: { title: 'Data missing or stale', detail: 'Market data unusable; downgraded to watch as the conservative fallback' },
  },
  intraday_unconfirmed_buy_blocked: {
    zh: { title: '盘中 K 线未收盘确认', detail: 'T+1 制度下盘中买入没有纠错权，否决新建买入' },
    en: { title: 'Intraday bar unconfirmed', detail: 'Under T+1 an intraday buy cannot be corrected; fresh buys blocked' },
  },
  weekly_bear_buy_blocked: {
    zh: { title: '周线空头结构', detail: '价格仍在 200 周线之下，周线过滤器否决新建买入' },
    en: { title: 'Weekly bear structure', detail: 'Price holds below the 200-week EMA; the weekly filter vetoes fresh buying' },
  },
  weekly_transition_buy_blocked: {
    zh: { title: '周线转换期', detail: '周线方向未定，买入降级（预留规则）' },
    en: { title: 'Weekly transition', detail: 'Weekly direction undecided; buys downgraded (reserved rule)' },
  },
  l1_disabled_buy_blocked: {
    zh: { title: '年线样本不足', detail: '样本不够，不信任趋势扩张，否决买入' },
    en: { title: 'Annual line sample too small', detail: 'Insufficient samples to trust trend expansion; buys blocked' },
  },
  coiling_transitioning_buy_downgraded: {
    zh: { title: '阶段处于收敛/切换', detail: '此阶段方向未明，不追买，买入降级为观望' },
    en: { title: 'Coiling / transitioning phase', detail: 'Direction unclear in this phase; no chasing, buys downgraded to watch' },
  },
  tiring_buy_downgraded: {
    zh: { title: '趋势动能疲惫', detail: '上涨动能先弱，不追买，买入降级为观望' },
    en: { title: 'Trend tiring', detail: 'Momentum weakening first; no chasing, buys downgraded to watch' },
  },
  resting_sell_blocked: {
    zh: { title: '主趋势段只是休整', detail: '休整是回踩而非反转，否决卖出' },
    en: { title: 'Segment merely resting', detail: 'A pullback is not a reversal; sells blocked' },
  },
  l2_broken_bear_allow_sell: {
    zh: { title: '空头结构破坏', detail: '年线为空、主趋势段破坏且当日收盘确认，允许离场' },
    en: { title: 'Bearish structure broken', detail: 'Annual bear + segment broken + confirmed close; exit allowed' },
  },
  l2_broken_bull_sell_blocked: {
    zh: { title: '多头段内正常回调', detail: '破坏发生在多头段内部，属于回调而非反转，否决恐慌卖出' },
    en: { title: 'Pullback inside a bull segment', detail: 'The break is a pullback, not a reversal; panic sells blocked' },
  },
  l1_l2_bear_sell_default: {
    zh: { title: '年线与趋势段双空头', detail: '双重空头结构，默认卖出' },
    en: { title: 'Double bear (annual + segment)', detail: 'Dual-bear structure; default to sell' },
  },
  l3_exhausted_sell_downgraded: {
    zh: { title: '节奏已失配', detail: '节奏层不支持顺势卖出，卖出降级为观望' },
    en: { title: 'Rhythm exhausted', detail: 'The rhythm layer does not support selling; sell downgraded to watch' },
  },
  l1_l2_l3_healthy_buy_allowed: {
    zh: { title: '全多头健康结构', detail: '三层全多头，屏蔽与趋势冲突的卖出' },
    en: { title: 'Healthy full-bull stack', detail: 'All three layers bullish; trend-fighting sells blocked' },
  },
  market_guardrail_softened: {
    zh: { title: '大盘环境护栏', detail: '大盘环境不佳，已软化买入动作' },
    en: { title: 'Market-context guardrail', detail: 'Weak market context softened the buy' },
  },
  phase_guardrail_suppressed: {
    zh: { title: '交易时段护栏', detail: '当前交易时段不支持该动作，已压制' },
    en: { title: 'Phase guardrail', detail: 'The current trading phase does not support the action; suppressed' },
  },
  bottom_fishing_time_stop: {
    zh: { title: '时间止损纪律', detail: '硬止损按失效价执行，另设 5 个交易日时间止损，不反弹即离场' },
    en: { title: 'Time-stop discipline', detail: 'Hard stop at the invalidation price plus a 5-trading-day time stop: exit if no bounce' },
  },
  weekly_bear_bottom_fishing_pass: {
    zh: { title: '空头下抄底放行', detail: '周线空头下的超卖抄底信号放行：属逆势博弈仓，轻仓严格止损，与趋势仓位分开管理；仅解除周线否决，阶段/样本等其余硬约束照常生效' },
    en: { title: 'Bottom-fishing pass under weekly bear', detail: 'Oversold bottom-fishing signal passes under weekly bear: a counter-trend sleeve, light size with strict stops, managed apart from trend positions; only the weekly veto is lifted — phase/sample hard constraints still apply' },
  },
  top_escape_exhaustion: {
    zh: { title: '超买衰竭', detail: 'RSI 升至 75 以上且 EMA20 斜率转负，上攻动能衰竭' },
    en: { title: 'Overbought exhaustion', detail: 'RSI above 75 with the EMA20 slope turning negative; upside momentum spent' },
  },
  top_escape_divergence: {
    zh: { title: '顶背离', detail: '价格新高但 RSI 高点下移，动能与价格背离' },
    en: { title: 'Bearish divergence', detail: 'Price makes a new high while the RSI high shifts down' },
  },
  top_escape_ema_loss: {
    zh: { title: '跌破 EMA20', detail: '收盘跌破 EMA20 且斜率转负、RSI 跌出强势区，趋势转弱确认' },
    en: { title: 'EMA20 lost', detail: 'Close below the EMA20 with a negative slope and RSI out of the strong zone' },
  },
};

// L4 timing signal labels (plan §13.8); mirrors backend i18n timing tables.
const TIMING_TYPE_TEXT: Record<string, Record<Lang, string>> = {
  pullback_entry: { zh: '顺势回踩入场', en: 'Pullback entry' },
  bottom_fishing: { zh: '超卖抄底', en: 'Bottom fishing' },
  top_escape: { zh: '高位逃顶', en: 'Top escape' },
};

const TIMING_STATUS_TEXT: Record<string, Record<Lang, string>> = {
  armed: { zh: '待触发', en: 'Armed' },
  triggered: { zh: '已触发', en: 'Triggered' },
};

const POSITION_HINT_TEXT: Record<string, Record<Lang, string>> = {
  light: { zh: '轻仓', en: 'Light size' },
  medium: { zh: '中等仓位', en: 'Medium size' },
  full: { zh: '仓位上限内', en: 'Within position cap' },
};

// ----- Principle explainer (白话为主 + 关键技术参数括注; mirrors plan §4/§13.8) -----

interface PrincipleBlock {
  heading?: Record<Lang, string>;
  text?: Record<Lang, string>;
  items?: Record<Lang, string>[];
}

interface PrincipleSection {
  id: string;
  title: Record<Lang, string>;
  blocks: PrincipleBlock[];
}

const PRINCIPLE_SECTIONS: PrincipleSection[] = [
  {
    id: 'what',
    title: { zh: '这是什么：两个问题与一条边界', en: 'What it is: two questions, one boundary' },
    blocks: [
      {
        text: {
          zh: '趋势罗盘用「周线过滤 + 日线三层结构」回答两个中期问题：这轮趋势还在不在？仓位该有多重？日线噪音（盘中波动、单日涨跌）被过滤掉，只留下周线到月级别的判断。',
          en: 'The compass answers two midterm questions with a weekly filter plus three daily layers: is this trend still alive, and how heavy should the position be? Daily noise (intraday swings, single-day moves) is filtered out, leaving week-to-month judgements only.',
        },
      },
      {
        text: {
          zh: '边界：本模块不做无约束的短期价格预测。择时信号是「条件触发的交易计划」（触发价 + 失效价 + 条件说明），跌破失效价计划立即作废——它是纪律工具，不是水晶球。',
          en: 'Boundary: this module makes no unconstrained short-term price predictions. Timing signals are condition-triggered trade plans (trigger + invalidation price + conditions); once the invalidation price trades the plan is void — a discipline tool, not a crystal ball.',
        },
      },
      {
        heading: { zh: '用到的技术参数', en: 'Technical parameters' },
        items: [
          {
            zh: '日线均线簇 EMA20 / EMA50 / EMA100 / EMA200；动量指标 RSI(14)',
            en: 'Daily EMA cluster EMA20 / EMA50 / EMA100 / EMA200; momentum RSI(14)',
          },
          {
            zh: '周线 EMA50 / EMA200（多头与仓位的总闸）',
            en: 'Weekly EMA50 / EMA200 (the master gate for trend and position size)',
          },
          {
            zh: '数据为前复权日线，默认窗口约 5 年（1200 个交易日）',
            en: 'Adjusted (qfq) daily bars, default window ~5 years (1200 trading days)',
          },
        ],
      },
    ],
  },
  {
    id: 'layers',
    title: { zh: '四层结构：各管一段时间', en: 'Four layers, each with its own time window' },
    blocks: [
      {
        text: {
          zh: '四层各管一段时间、互相独立——一层变差只影响它自己那部分结论。',
          en: 'Each layer covers its own time window and is independent — deterioration in one only affects its own part of the verdict.',
        },
      },
      {
        items: [
          {
            zh: 'L0 周线过滤（最硬的一道闸）：价格站在 200 周线之上且 50 周线向上，才允许在仓位上限内买入；价格压在 200 周线下 = 周线空头，默认禁止新建买入。仓位上限三档：允许重仓波段 / 半仓上限 / 禁止买入。周 K 样本不足（<60 根）时本层退出决策。',
            en: 'L0 Weekly filter (the hardest gate): buying is allowed only above the 200-week EMA with the 50-week EMA rising, and only within the position cap; below the 200-week EMA is weekly bear — fresh buys blocked by default. Three cap levels: full / half / none. With fewer than 60 weekly bars this layer steps out of the decision.',
          },
          {
            zh: 'L1 年线（半年-一年）：牛熊大背景。年线为空整体偏防守；样本不足时「不信任趋势扩张」，直接否决买入。',
            en: 'L1 Annual (6-12 months): the bull/bear backdrop. Annual bear is defensive overall; with insufficient samples it distrusts trend expansion and blocks buys outright.',
          },
          {
            zh: 'L2 主趋势段（1-3 个月）：趋势是否还活着。alive=仍在；resting=回踩休整（是回踩不是反转，故休整段禁止恐慌卖出）；broken=结构破坏（若年线同为空头且收盘确认，允许离场）。',
            en: 'L2 Segment (1-3 months): whether the trend is still alive. alive=yes; resting=a pullback (not a reversal, so panic sells are blocked); broken=structure break (exit allowed when the annual layer is also bearish and the close confirms).',
          },
          {
            zh: 'L3 节奏（2-6 周）：管加速度不管方向。healthy=健康；cooling=降温；exhausted=失配（动能先弱，不追买，卖出也降级）；noisy=数据嘈杂。',
            en: 'L3 Rhythm (2-6 weeks): governs acceleration, not direction. healthy / cooling / exhausted (momentum fades first — no chasing, sells downgraded too) / noisy.',
          },
        ],
      },
      {
        text: {
          zh: '阶段合成：四层状态合成五个阶段（扩张 / 持有 / 疲惫 / 收敛 / 切换），决定你看多远：执行窗 1 周、节奏窗 2 周、主趋势窗 1 个月。收敛与切换阶段方向未明，所有买入一律降级为观望。',
          en: 'Phase synthesis: the four layers compose into five phases (expanding / holding / tiring / coiling / transitioning), which set the horizon: 1-week execution, 2-week rhythm, 1-month segment window. In coiling and transitioning phases direction is unclear — every buy is downgraded to watch.',
        },
      },
    ],
  },
  {
    id: 'rewrite',
    title: { zh: '动作改写：为什么不直接信「建议买入」', en: 'Action rewrite: why a draft is not the final call' },
    blocks: [
      {
        text: {
          zh: '原则：初稿动作（例如模型给出的建议）只是「建议」，必须逐条通过硬约束链。约束只能否决或降级，永远不能把「观望」升级成「买入」。每触发一条，改写面板就显示一行中文解释，最终结论以链条末端为准。',
          en: 'Principle: a draft action (e.g. a model suggestion) is only a suggestion and must pass the hard-constraint chain rule by rule. Constraints can only veto or downgrade — they can never upgrade "watch" into "buy". Each fired rule shows one explained line in the rewrite panel; the chain end is the final call.',
        },
      },
      {
        heading: { zh: '三个实例', en: 'Three examples' },
        items: [
          {
            zh: '否决链：初稿「买入」+ 周线空头 → 否决为观望；若年线、趋势段同为空头 → 链条继续，默认改为「卖出」。600519 真实行情就是这种形态。',
            en: 'Veto chain: draft "buy" + weekly bear → vetoed to watch; if the annual layer and segment are also bearish → the chain continues and defaults to "sell". 600519 in live data is exactly this shape.',
          },
          {
            zh: '保护性否决：初稿「卖出」+ 多头趋势段只是休整 → 否决（回踩不是反转，不割在坑里）。',
            en: 'Protective veto: draft "sell" while a bull segment is merely resting → vetoed (a pullback is not a reversal; do not sell into the hole).',
          },
          {
            zh: '放行例外：周线空头下出现超卖抄底信号，买入可被放行为逆势博弈仓——但仅解除周线这一条否决，阶段/样本等其余约束照常生效。',
            en: 'Pass exception: under weekly bear an oversold bottom-fishing signal can pass as a counter-trend sleeve — but only the weekly veto is lifted; phase/sample constraints still apply.',
          },
        ],
      },
    ],
  },
  {
    id: 'timing',
    title: { zh: '择时信号：条件触发的交易计划', en: 'Timing signals: condition-triggered trade plans' },
    blocks: [
      {
        text: {
          zh: '三种信号共用同一个形态：触发价（计划生效参考）、失效价（跌破即作废）、有效期（按阶段 5-20 个交易日）、仓位提示。它们只在收盘确认的 K 线上出现，盘中不出信号。',
          en: 'All three signals share one form: trigger price (plan activation reference), invalidation price (void once traded), validity (5-20 trading days by phase), and a position hint. They appear only on confirmed closing bars — never intraday.',
        },
      },
      {
        items: [
          {
            zh: '顺势回踩入场：上涨趋势中回踩 EMA20（盘中触及 ±1%）且 RSI 回到 40-55 的复位区、收盘收复 EMA20、短期斜率向上 → 触发。失效价 = 回踩低点下方 1%。',
            en: 'Pullback entry: in an uptrend, price dips into the EMA20 zone (within ±1%) with RSI reset to the 40-55 band, then closes back above the EMA20 with a positive short-term slope → triggered. Invalidation = 1% below the pullback low.',
          },
          {
            zh: '超卖抄底：RSI 跌破 30 + 底背离（价格新低而 RSI 低点抬高）+ 止跌阳线。周线空头下也出信号，但仓位提示固定「轻仓」——逆势博弈仓，与趋势仓位分开管理；硬止损按失效价，另设 5 个交易日时间止损，不反弹即离场。',
            en: 'Bottom fishing: RSI below 30 + bullish divergence (price new low while the RSI low rises) + a stabilizing up close. It also fires under weekly bear, but the position hint is fixed at "light" — a counter-trend sleeve managed apart from trend positions; hard stop at the invalidation price plus a 5-trading-day time stop, exit if no rebound.',
          },
          {
            zh: '高位逃顶：超买衰竭（RSI>75 且斜率转负）/ 顶背离（价格新高而 RSI 不新高）/ 跌破 EMA20 且斜率转负 → 触发。失效价 = 前高上方 1%：若价格再创新高，说明判断错了，计划作废。全多头健康结构下不逃顶。',
            en: 'Top escape: overbought exhaustion (RSI>75 with the slope turning negative) / bearish divergence (price new high, RSI not) / losing the EMA20 with a negative slope → triggered. Invalidation = 1% above the prior high: a new high means the call was wrong and the plan is void. No escape signals inside a healthy full-bull stack.',
          },
        ],
      },
      {
        text: {
          zh: '纪律：跌破失效价立即作废，不补仓、不扛单。',
          en: 'Discipline: the plan is void the moment the invalidation price trades — no averaging down, no holding through stops.',
        },
      },
    ],
  },
  {
    id: 'discipline',
    title: { zh: '失效条件与数据纪律', en: 'Invalidation & data discipline' },
    blocks: [
      {
        items: [
          {
            zh: '整卡作废：任一失效条件触发（周线转空、趋势段破坏、节奏持续失配），本卡结论立即作废，等下一根收盘确认再评估。',
            en: 'Whole-card invalidation: if any invalidation condition fires (weekly turns bear, segment breaks, rhythm stays exhausted), this card is void immediately — re-evaluate only after the next confirmed close.',
          },
          {
            zh: '盘中只展示、不出信号：未收盘的 K 线可能反转（T+1 无纠错权），所有买入类信号都要求收盘确认。',
            en: 'Intraday is display-only: an unclosed bar can still reverse (no correction right under T+1); every buy-side signal requires a confirmed close.',
          },
          {
            zh: '样本不足宁缺毋滥：哪层样本不够，哪层标记「样本不足」并退出决策——缺数据比错数据好。',
            en: 'When samples are insufficient the layer is marked "insufficient sample" and steps out — missing data beats wrong data.',
          },
          {
            zh: '各模块独立：缠论、波浪等其他模块不读罗盘，结论冲突时以各自免责声明为准。',
            en: 'Modules are independent: Chan theory, Elliott wave and others do not read the compass; when conclusions conflict, each module\'s own disclaimer governs.',
          },
        ],
      },
    ],
  },
];

// 全量硬约束表（§4.5.2，14 条），原则 + 实例之外的速查区。
const CONSTRAINT_TABLE: { no: string; condition: Record<Lang, string>; action: Record<Lang, string>; note: Record<Lang, string> }[] = [
  { no: '1', condition: { zh: '数据缺失或停滞', en: 'Data missing or stale' }, action: { zh: '观望', en: 'Watch' }, note: { zh: '行情不可用，按最保守处理', en: 'Unusable data falls back to the most conservative call' } },
  { no: '2', condition: { zh: '盘中 K 线未收盘确认', en: 'Intraday bar unconfirmed' }, action: { zh: '买入 → 观望', en: 'Buy → Watch' }, note: { zh: 'T+1 下盘中买入没有纠错权', en: 'No correction right for intraday buys under T+1' } },
  { no: '3', condition: { zh: '周线空头', en: 'Weekly bear' }, action: { zh: '买入 → 观望', en: 'Buy → Watch' }, note: { zh: '价格在 200 周线下；超卖抄底信号除外（逆势博弈仓放行）', en: 'Below the 200-week EMA; except bottom-fishing signals (counter-trend sleeve)' } },
  { no: '4', condition: { zh: '周线转换期', en: 'Weekly transition' }, action: { zh: '不改写动作', en: 'No rewrite' }, note: { zh: '方向未定，仅降级置信度', en: 'Direction unclear; confidence downgraded only' } },
  { no: '5', condition: { zh: '年线样本不足', en: 'Annual sample too small' }, action: { zh: '买入 → 观望', en: 'Buy → Watch' }, note: { zh: '样本不够，不信任趋势扩张', en: 'Insufficient samples to trust trend expansion' } },
  { no: '6', condition: { zh: '收敛 / 切换阶段', en: 'Coiling / transitioning phase' }, action: { zh: '买入 → 观望', en: 'Buy → Watch' }, note: { zh: '方向未明，不追买', en: 'Direction unclear; no chasing' } },
  { no: '7', condition: { zh: '趋势疲惫', en: 'Trend tiring' }, action: { zh: '买入 → 观望', en: 'Buy → Watch' }, note: { zh: '上涨动能先弱，不追买', en: 'Momentum fades first; no chasing' } },
  { no: '8', condition: { zh: '趋势段休整', en: 'Segment resting' }, action: { zh: '卖出 → 观望', en: 'Sell → Watch' }, note: { zh: '休整是回踩而非反转', en: 'A pullback is not a reversal' } },
  { no: '9', condition: { zh: '空头结构破坏（年线空 + 段破坏 + 收盘确认）', en: 'Bearish break (annual bear + segment broken + confirmed close)' }, action: { zh: '观望/买入 → 卖出', en: 'Watch/Buy → Sell' }, note: { zh: '允许离场', en: 'Exit allowed' } },
  { no: '10', condition: { zh: '多头段内破坏', en: 'Break inside a bull segment' }, action: { zh: '卖出 → 观望', en: 'Sell → Watch' }, note: { zh: '属回调而非反转，禁止恐慌卖出', en: 'A pullback, not a reversal; panic sells blocked' } },
  { no: '11', condition: { zh: '年线 + 趋势段双空头', en: 'Annual + segment double bear' }, action: { zh: '观望 → 卖出', en: 'Watch → Sell' }, note: { zh: '双重空头默认卖出；节奏失配时降回观望', en: 'Default sell; downgraded back to watch when rhythm is exhausted' } },
  { no: '12', condition: { zh: '全多头健康结构', en: 'Healthy full-bull stack' }, action: { zh: '卖出 → 观望', en: 'Sell → Watch' }, note: { zh: '屏蔽与趋势冲突的卖出', en: 'Trend-fighting sells blocked' } },
  { no: '13', condition: { zh: '大盘环境护栏', en: 'Market-context guardrail' }, action: { zh: '仅记录', en: 'Audit note' }, note: { zh: '大盘不佳时的软化提示，不改变罗盘结论', en: 'Softening note; does not change the compass verdict' } },
  { no: '14', condition: { zh: '交易时段护栏', en: 'Phase guardrail' }, note: { zh: '当前时段不支持该动作的压制提示', en: 'Suppression note when the session does not support the action' }, action: { zh: '仅记录', en: 'Audit note' } },
];

const LAYER_HEADINGS: { key: 'weekly' | 'annual' | 'segment' | 'rhythm'; zh: string; en: string }[] = [
  { key: 'weekly', zh: 'L0 周线过滤', en: 'L0 Weekly filter' },
  { key: 'annual', zh: 'L1 年线（半年-一年）', en: 'L1 Annual filter' },
  { key: 'segment', zh: 'L2 主趋势段（1-3 个月）', en: 'L2 Segment (1-3 months)' },
  { key: 'rhythm', zh: 'L3 节奏（2-6 周）', en: 'L3 Rhythm (2-6 weeks)' },
];

const EXAMPLE_CODES = ['600519', '300750', '002594', 'sh000300'];

const fmt = (value: number | null | undefined, digits = 2): string =>
  typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';

export default function TrendCompassPage() {
  const { t, language } = useUiLanguage();
  const lang: Lang = language === 'en' ? 'en' : 'zh';
  const [query, setQuery] = useState('');
  const [selectedName, setSelectedName] = useState<string | undefined>(undefined);
  const [draft, setDraft] = useState<CompassAction | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CompassAnalyzeResult | null>(null);

  const analyze = async (code: string, name?: string) => {
    const normalized = code.trim();
    if (!normalized || loading) return;
    setLoading(true);
    setError(null);
    try {
      const data = await trendCompassApi.analyze({
        code: normalized,
        stockName: name ?? selectedName,
        draftAction: draft,
        lang,
      });
      setResult(data);
    } catch (err) {
      setResult(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const compass = result?.compass;
  // 防御旧后端/异常 payload：老版本 API 可能不下发 timing 字段。
  const timing = compass?.timing ?? [];
  const finalAction: CompassAction | null = result?.rewrite?.final_action ?? null;
  const rewriteSteps = result?.rewrite?.steps ?? [];

  return (
    <AppPage>
      <PageHeader
        title={t('layout.nav.trendCompass')}
        description={t('trendCompass.subtitle')}
      />

      <Card className="mt-4">
        <div className="flex flex-col gap-3">
          <StockAutocomplete
            value={query}
            onChange={setQuery}
            onSubmit={(code, name) => {
              setSelectedName(name);
              setQuery(code);
              void analyze(code, name);
            }}
            disabled={loading}
            placeholder={t('trendCompass.inputPlaceholder')}
          />
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-secondary-text">{t('trendCompass.draftLabel')}</span>
            {([null, 'buy', 'watch', 'sell'] as const).map((option) => (
              <button
                key={option ?? 'none'}
                type="button"
                onClick={() => setDraft(option)}
                className={cn(
                  'rounded-full border px-3 py-1 text-xs transition-colors',
                  draft === option
                    ? 'border-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary))]'
                    : 'border-border/60 text-secondary-text hover:border-border'
                )}
              >
                {option === null ? t('trendCompass.draftNone') : ACTION_TEXT[option][lang]}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap gap-2">
            {EXAMPLE_CODES.map((code) => (
              <button
                key={code}
                type="button"
                onClick={() => {
                  setQuery(code);
                  setSelectedName(undefined);
                  void analyze(code);
                }}
                disabled={loading}
                className="rounded-full border border-border/60 px-3 py-1 text-xs text-secondary-text transition-colors hover:border-border hover:text-foreground"
              >
                {code}
              </button>
            ))}
          </div>
          <div>
            <Button onClick={() => void analyze(query)} disabled={loading || !query.trim()}>
              {loading ? t('trendCompass.analyzing') : t('trendCompass.analyze')}
            </Button>
          </div>
        </div>
      </Card>

      <Collapsible
        title={lang === 'en' ? 'How the compass works (principles)' : '罗盘原理说明'}
        maxHeightPx={8000}
        className="mt-4"
      >
        <p className="text-xs text-secondary-text">
          {lang === 'en'
            ? 'Methodology in plain language: what each layer does, how the rewrite chain works, and what timing signals really mean.'
            : '白话版方法论：每层在干什么、改写链怎么工作、择时信号到底意味着什么。'}
        </p>
        {/* 框内迷你目录：单框长文的站内导航（锚点跳转） */}
        <nav className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {PRINCIPLE_SECTIONS.map((section) => (
            <a
              key={section.id}
              href={`#principle-${section.id}`}
              className="text-[hsl(var(--primary))] hover:underline"
            >
              {section.title[lang]}
            </a>
          ))}
        </nav>
        {PRINCIPLE_SECTIONS.map((section, sIdx) => (
          <div key={section.id} className={sIdx > 0 ? 'mt-4 border-t border-border/40 pt-3' : 'mt-3'}>
            <h3 id={`principle-${section.id}`} className="scroll-mt-4 text-sm font-semibold text-foreground">
              {section.title[lang]}
            </h3>
            <div className="mt-2 space-y-2 text-sm">
              {section.blocks.map((block, idx) => (
                <div key={idx}>
                  {block.heading ? (
                    <p className="mb-1 font-medium text-foreground">{block.heading[lang]}</p>
                  ) : null}
                  {block.text ? (
                    <p className="text-secondary-text">{block.text[lang]}</p>
                  ) : null}
                  {block.items ? (
                    <ul className="list-disc space-y-1.5 pl-5 text-secondary-text">
                      {block.items.map((item, j) => (
                        <li key={j}>{item[lang]}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ))}
            </div>
            {section.id === 'rewrite' ? (
              <div className="mt-3 overflow-x-auto">
                <p className="mb-1 text-xs font-medium text-foreground">
                  {lang === 'en' ? 'Full hard-constraint table (14 rules)' : '全部 14 条硬约束速查表'}
                </p>
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border text-left text-secondary-text">
                      <th className="py-1 pr-2 font-medium">#</th>
                      <th className="py-1 pr-2 font-medium">{lang === 'en' ? 'When' : '触发条件'}</th>
                      <th className="py-1 pr-2 font-medium">{lang === 'en' ? 'Action' : '动作'}</th>
                      <th className="py-1 font-medium">{lang === 'en' ? 'Why' : '说明'}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {CONSTRAINT_TABLE.map((row) => (
                      <tr key={row.no} className="border-b border-border/40 align-top">
                        <td className="py-1.5 pr-2 text-secondary-text">{row.no}</td>
                        <td className="py-1.5 pr-2 text-foreground">{row.condition[lang]}</td>
                        <td className="py-1.5 pr-2 text-foreground">{row.action[lang]}</td>
                        <td className="py-1.5 text-secondary-text">{row.note[lang]}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        ))}
      </Collapsible>

      {loading ? <Loading className="mt-8" /> : null}

      {error ? (
        <Card className="mt-4 border-rose-500/40">
          <p className="text-sm text-rose-600 dark:text-rose-400">{error}</p>
        </Card>
      ) : null}

      {!loading && !error && !result ? (
        <EmptyState
          className="mt-8"
          title={t('trendCompass.emptyTitle')}
          description={t('trendCompass.emptyHint')}
        />
      ) : null}

      {compass ? (
        <div className="mt-4 flex flex-col gap-4">
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <h2 className="text-lg font-semibold text-foreground">
                    {compass.name ?? compass.code}
                  </h2>
                  <span className="text-sm text-secondary-text">{compass.code}</span>
                </div>
                <p className="mt-1 text-xs text-secondary-text">
                  {t('trendCompass.asOf')} {compass.as_of_trade_date} ·{' '}
                  {BAR_STATUS_TEXT[compass.bar_status]?.[lang] ?? compass.bar_status}
                </p>
              </div>
              {finalAction ? (
                <span className={cn('rounded-2xl border px-5 py-2 text-xl font-semibold', ACTION_CLASS[finalAction])}>
                  {ACTION_TEXT[finalAction][lang]}
                </span>
              ) : null}
            </div>
            {compass.quality.limitations.length > 0 ? (
              <div className="mt-3 flex flex-wrap gap-2">
                {compass.quality.limitations.map((item) => (
                  <Badge key={item} variant="warning">{item}</Badge>
                ))}
              </div>
            ) : null}
          </Card>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {LAYER_HEADINGS.map(({ key, zh, en }) => (
              <Card key={key} title={lang === 'en' ? en : zh} padding="sm">
                <p className="text-sm font-medium text-foreground">
                  {LAYER_TEXT[compass[key]]?.[lang] ?? compass[key]}
                </p>
              </Card>
            ))}
          </div>

          <Card title={lang === 'en' ? 'Phase synthesis' : '阶段合成'}>
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-base font-semibold text-foreground">
                {PHASE_TEXT[compass.phase]?.[lang] ?? compass.phase}
              </span>
              <Badge variant="info">{HORIZON_TEXT[compass.observe_horizon]?.[lang] ?? compass.observe_horizon}</Badge>
              <Badge variant="default">{POSITION_TEXT[compass.position_filter]?.[lang] ?? compass.position_filter}</Badge>
            </div>
          </Card>

          {result?.rewrite ? (
            <Card
              title={lang === 'en' ? 'Action rewrite · hard-constraint rules' : '动作改写 · 硬约束规则'}
            >
              {/* 规则表出处（§4.5.2）留在 tooltip，界面不显示文档编号；原始 reason code 也仅在 hover 时可见 */}
              <Tooltip content={lang === 'en' ? 'Hard-constraint table: plan §4.5.2' : '规则表出处：设计文档 §4.5.2'} className="w-full">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-secondary-text">{lang === 'en' ? 'Draft: ' : '初稿动作：'}</span>
                  <span className={cn('rounded-lg border px-3 py-1', ACTION_CLASS[result.rewrite.initial_action])}>
                    {ACTION_TEXT[result.rewrite.initial_action][lang]}
                  </span>
                </div>
                {rewriteSteps.length > 0 ? (
                  <ol className="mt-3 space-y-2 text-sm">
                    {rewriteSteps.map((step, idx) => {
                      const prev =
                        idx === 0 ? result.rewrite!.initial_action : rewriteSteps[idx - 1].action_after;
                      const info = REASON_STEP_TEXT[step.reason_code]?.[lang];
                      const changed = prev !== step.action_after;
                      const transition = changed
                        ? lang === 'zh'
                          ? `，动作由「${ACTION_TEXT[prev].zh}」改为「${ACTION_TEXT[step.action_after].zh}」`
                          : ` — action changed from ${ACTION_TEXT[prev].en} to ${ACTION_TEXT[step.action_after].en}`
                        : lang === 'zh'
                          ? `，动作维持「${ACTION_TEXT[step.action_after].zh}」`
                          : ` — action stays ${ACTION_TEXT[step.action_after].en}`;
                      return (
                        <li
                          key={`${step.reason_code}-${idx}`}
                          title={step.reason_code}
                          className="flex gap-2"
                        >
                          <span className="shrink-0 font-semibold text-secondary-text">{idx + 1}.</span>
                          <span>
                            <span className="font-medium text-foreground">{info?.title ?? step.reason_code}</span>
                            {info?.detail ? (
                              <span className="text-secondary-text"> —— {info.detail}</span>
                            ) : null}
                            <span className="text-secondary-text">{transition}</span>
                          </span>
                        </li>
                      );
                    })}
                  </ol>
                ) : (
                  <p className="mt-3 text-sm text-secondary-text">
                    {lang === 'en'
                      ? 'No hard constraint triggered; the draft passes through unchanged.'
                      : '未命中任何硬约束，初稿原样通过。'}
                  </p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3 text-sm">
                  <span className="text-secondary-text">{lang === 'en' ? 'Final: ' : '最终结果：'}</span>
                  <span className={cn('rounded-lg border px-3 py-1', ACTION_CLASS[result.rewrite.final_action])}>
                    {ACTION_TEXT[result.rewrite.final_action][lang]}
                  </span>
                  <Badge variant="default">
                    {lang === 'en' ? 'Conclusion: ' : '投资结论：'}
                    {result.rewrite.investment_action}
                  </Badge>
                </div>
                <p className="mt-2 text-xs text-secondary-text">
                  {lang === 'en' ? 'Basis: ' : '判定依据：'}
                  {[
                    [lang === 'en' ? 'Weekly' : '周线', LAYER_TEXT[compass.weekly]?.[lang] ?? compass.weekly],
                    [lang === 'en' ? 'Annual' : '年线', LAYER_TEXT[compass.annual]?.[lang] ?? compass.annual],
                    [lang === 'en' ? 'Segment' : '主趋势段', LAYER_TEXT[compass.segment]?.[lang] ?? compass.segment],
                    [lang === 'en' ? 'Rhythm' : '节奏', LAYER_TEXT[compass.rhythm]?.[lang] ?? compass.rhythm],
                    [lang === 'en' ? 'Phase' : '阶段', PHASE_TEXT[compass.phase]?.[lang] ?? compass.phase],
                    [lang === 'en' ? 'Bar' : 'K线', BAR_STATUS_TEXT[compass.bar_status]?.[lang] ?? compass.bar_status],
                  ]
                    .map(([k, v]) => `${k}=${v}`)
                    .join(' · ')}
                </p>
              </Tooltip>
            </Card>
          ) : null}

          <Card title={lang === 'en' ? 'Timing signals · daily L4' : '择时信号 · 日线 L4'}>
            {/* 择时信号是条件触发的交易计划（触发价+失效价），不是价格预测；§13.8 */}
            {timing.length === 0 ? (
              <p className="text-sm text-secondary-text">
                {lang === 'en'
                  ? 'No timing signals currently. A signal appears only when the daily setup (pullback reset / oversold divergence / overbought exhaustion) aligns with the trend gates.'
                  : '当前无择时信号。仅在日线形态（回踩复位 / 超卖背离 / 超买衰竭）与趋势门控同时满足时出现。'}
              </p>
            ) : (
              <div className="space-y-3">
                {timing.map((signal, idx) => (
                  <div
                    key={`${signal.type}-${idx}`}
                    className="rounded-lg border border-border/60 p-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-semibold text-foreground">
                        {TIMING_TYPE_TEXT[signal.type]?.[lang] ?? signal.type}
                      </span>
                      <Badge
                        variant={signal.status === 'triggered'
                          ? (signal.type === 'top_escape' ? 'danger' : 'success')
                          : 'default'}
                      >
                        {TIMING_STATUS_TEXT[signal.status]?.[lang] ?? signal.status}
                      </Badge>
                      {signal.countertrend ? (
                        <Badge variant="warning">
                          {lang === 'en'
                            ? 'Counter-trend sleeve: separate from trend positions'
                            : '逆势博弈仓：与趋势仓位分开管理'}
                        </Badge>
                      ) : null}
                    </div>
                    {signal.reason_codes.length > 0 ? (
                      <ul className="mt-2 space-y-1 text-sm">
                        {signal.reason_codes.map((code) => {
                          const info = REASON_STEP_TEXT[code]?.[lang];
                          return (
                            <li key={code} title={code} className="text-secondary-text">
                              <span className="font-medium text-foreground">{info?.title ?? code}</span>
                              {info?.detail ? <span> —— {info.detail}</span> : null}
                            </li>
                          );
                        })}
                      </ul>
                    ) : null}
                    <dl className="mt-2 grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
                      <div className="flex items-center justify-between border-b border-border/40 pb-1">
                        <dt className="text-secondary-text">{lang === 'en' ? 'Trigger' : '触发价'}</dt>
                        <dd className="font-medium text-foreground">
                          {signal.trigger_price !== null ? fmt(signal.trigger_price) : `—（${TIMING_STATUS_TEXT.armed[lang]}）`}
                        </dd>
                      </div>
                      <div className="flex items-center justify-between border-b border-border/40 pb-1">
                        <dt className="text-secondary-text">{lang === 'en' ? 'Invalidation' : '失效价'}</dt>
                        <dd className="font-medium text-foreground">
                          {signal.invalidation_price !== null ? fmt(signal.invalidation_price) : '—'}
                        </dd>
                      </div>
                      <div className="flex items-center justify-between border-b border-border/40 pb-1">
                        <dt className="text-secondary-text">{lang === 'en' ? 'Valid for' : '有效期'}</dt>
                        <dd className="font-medium text-foreground">
                          {signal.horizon_days} {lang === 'en' ? 'trading days' : '个交易日'}
                        </dd>
                      </div>
                    </dl>
                    <p className="mt-2 text-xs text-secondary-text">
                      {lang === 'en' ? 'Position hint: ' : '仓位提示：'}
                      {POSITION_HINT_TEXT[signal.position_hint]?.[lang] ?? signal.position_hint}
                    </p>
                  </div>
                ))}
                <p className="text-xs text-secondary-text">
                  {lang === 'en'
                    ? 'Timing signals are condition-triggered trade plans, not price predictions: the plan is void once the invalidation price trades — no averaging down, no holding through stops.'
                    : '择时信号是条件触发的交易计划，非价格预测：跌破失效价计划立即作废，不补仓、不扛单。'}
                </p>
              </div>
            )}
          </Card>

          <Card title={lang === 'en' ? 'Indicators' : '指标快照'}>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
              {(
                [
                  ['EMA20', fmt(compass.indicators.ema20)],
                  ['EMA50', fmt(compass.indicators.ema50)],
                  ['EMA100', fmt(compass.indicators.ema100)],
                  ['EMA200', fmt(compass.indicators.ema200)],
                  ['RSI(14)', fmt(compass.indicators.rsi14, 1)],
                  [lang === 'en' ? 'Close' : '收盘价', fmt(compass.indicators.price)],
                  ['EMA20 10d', fmt(compass.indicators.slope_ema20_10d, 3)],
                  ['EMA50 20d', fmt(compass.indicators.slope_ema50_20d, 3)],
                  ['EMA200 40d', fmt(compass.indicators.slope_ema200_40d, 3)],
                ] as const
              ).map(([label, value]) => (
                <div key={label} className="flex items-center justify-between border-b border-border/40 pb-1">
                  <dt className="text-secondary-text">{label}</dt>
                  <dd className="font-medium text-foreground">{value}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-3 text-xs text-secondary-text">{result?.short_card}</p>
          </Card>

          <Card title={lang === 'en' ? 'Full report' : '完整长卡'}>
            <ReportMarkdownBody content={result?.long_card ?? ''} />
            <p className="mt-4 border-t border-border/40 pt-3 text-xs text-secondary-text">
              {compass.disclaimer}
            </p>
          </Card>
        </div>
      ) : null}
    </AppPage>
  );
}
