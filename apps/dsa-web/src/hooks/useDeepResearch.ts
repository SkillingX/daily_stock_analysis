import { useCallback, useEffect, useRef, useState } from 'react';
import {
  deepResearchApi,
  type DeepResearchGuardrailEvent,
  type DeepResearchReportDetail,
} from '../api/deepResearch';

export type DeepResearchStatus = 'idle' | 'generating' | 'done' | 'error';

export interface DeepResearchProgressStep {
  type: string;
  step?: number;
  message?: string;
  tool?: string;
  display_name?: string;
  success?: boolean;
  /** 双轨引擎维度事件（dim_start/dim_done）携带 */
  dim?: string;
  status?: string;
}

/** 双轨引擎 11 维度（与后端 DIM_IDS 严格对齐，徽章展示用） */
export const DUAL_TRACK_DIMS: { id: string; label: string }[] = [
  { id: 'fundamental', label: '财务' },
  { id: 'sector', label: '板块' },
  { id: 'supply_chain', label: '产业链' },
  { id: 'intel', label: '消息' },
  { id: 'six_dim', label: '六维' },
  { id: 'bayesian', label: '贝叶斯' },
  { id: 'scenarios', label: '情景' },
  { id: 'conclusion', label: '结论' },
  { id: 'data', label: '数据' },
  { id: 'signal', label: '信号' },
  { id: 'plan', label: '计划' },
  { id: 'phase', label: '阶段' },
  { id: 'history', label: '历史' },
  { id: 'technical', label: '技术' },
  { id: 'capital', label: '资金' },
  { id: 'sentiment', label: '情绪' },
  { id: 'ownership', label: '股权' },
  { id: 'us_china', label: '中美' },
  { id: 'business', label: '业务' },
];

export type DimRunStatus = 'pending' | 'running' | 'ok' | 'degraded' | 'skipped';

export type DimStatusMap = Record<string, DimRunStatus>;

/** 双轨 done 事件的增量字段（legacy 引擎为空） */
export interface DualTrackExtras {
  engine?: 'legacy' | 'dual_track';
  dimensions?: Record<string, { status?: string; degraded_reason?: string }>;
  guardrailEvents: DeepResearchGuardrailEvent[];
  dimsDegraded: string[];
  /** 服务端日缓存命中（同一天同股票/同子集直接返回既有报告） */
  cacheHit?: boolean;
}

const INITIAL_DIM_STATUSES: DimStatusMap = Object.fromEntries(
  DUAL_TRACK_DIMS.map((d) => [d.id, 'pending' as const]),
);

/**
 * 深度投研报告生成 hook（表单流：一次性 SSE 生成）。
 *
 * 参考 agentChatStore 的 SSE 解析（fetch + ReadableStream + `data: ` 行），
 * 增强点：
 * - AbortController：用户可取消（cancel）。
 * - 心跳 watchdog：90s 内无任何事件（含 heartbeat）视为断线 → error。
 * - 双轨引擎维度徽章：dim_start/dim_done 事件维护 dimStatuses。
 * - 状态机：idle → generating → done | error。
 *
 * 注意：断线不做自动重连（一次性生成，重连=重复生成浪费）。断线提示用户
 * 去"历史列表"查看（报告可能已在后端生成落盘）。
 */
export function useDeepResearch() {
  const [status, setStatus] = useState<DeepResearchStatus>('idle');
  const [progressSteps, setProgressSteps] = useState<DeepResearchProgressStep[]>([]);
  const [report, setReport] = useState<DeepResearchReportDetail | null>(null);
  const [reportId, setReportId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dimStatuses, setDimStatuses] = useState<DimStatusMap>(INITIAL_DIM_STATUSES);
  const [dualTrack, setDualTrack] = useState<DualTrackExtras>({
    guardrailEvents: [],
    dimsDegraded: [],
    cacheHit: false,
  });

  const abortRef = useRef<AbortController | null>(null);
  const watchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 90s 无事件 = 断线（服务端每 30s 发心跳，90s 容忍 2 次心跳丢失）
  const WATCHDOG_MS = 90_000;

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current) {
      clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
  }, []);

  const resetWatchdog = useCallback(() => {
    clearWatchdog();
    watchdogRef.current = setTimeout(() => {
      if (abortRef.current) {
        abortRef.current.abort();
      }
      setStatus('error');
      setError('连接超时中断。若报告已生成，可在左侧历史列表查看。');
    }, WATCHDOG_MS);
  }, [clearWatchdog]);

  const generate = useCallback(
    async (
      stockCode: string,
      stockName?: string,
      options?: { dims?: string[]; forceRefresh?: boolean },
    ) => {
      // 重置状态
      setStatus('generating');
      setProgressSteps([]);
      setReport(null);
      setReportId(null);
      setError(null);
      setDimStatuses(INITIAL_DIM_STATUSES);
      setDualTrack({ guardrailEvents: [], dimsDegraded: [], cacheHit: false });

      const ac = new AbortController();
      abortRef.current = ac;
      resetWatchdog();

      try {
        const response = await deepResearchApi.generateStream(
          {
            stock_code: stockCode,
            stock_name: stockName,
            report_type: 'deep',
            dims: options?.dims,
            force_refresh: options?.forceRefresh,
          },
          { signal: ac.signal },
        );
        const reader = response.body?.getReader();
        if (!reader) {
          throw new Error('SSE 流不可用');
        }
        const decoder = new TextDecoder();
        let buf = '';
        let doneReceived = false;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop() ?? '';

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            // 收到任意事件重置 watchdog（含心跳）
            resetWatchdog();

            let event: DeepResearchProgressStep & {
              report_id?: string;
              markdown?: string;
              status?: string;
              quality_score?: number;
              missing_layers?: string[];
              message?: string;
              error?: string;
              success?: boolean;
              dim?: string;
              engine?: 'legacy' | 'dual_track';
              dimensions?: Record<string, { status?: string; degraded_reason?: string }>;
              guardrail_events?: DeepResearchGuardrailEvent[];
              dims_degraded?: string[];
              cache_hit?: boolean;
            };
            try {
              event = JSON.parse(line.slice(6));
            } catch {
              continue;
            }

            if (event.type === 'done') {
              doneReceived = true;
              const rid = event.report_id ?? null;
              setReportId(rid);
              setReport({
                id: rid ?? '',
                stock_code: stockCode,
                stock_name: stockName || stockCode,
                markdown: event.markdown || '',
                status: event.status,
                quality_score: event.quality_score,
                missing_layers: event.missing_layers || [],
                engine: event.engine,
              });
              setDualTrack((prev) => ({ ...prev, cacheHit: Boolean(event.cache_hit) }));
              if (event.engine === 'dual_track') {
                setDualTrack({
                  engine: event.engine,
                  cacheHit: Boolean(event.cache_hit),
                  dimensions: event.dimensions,
                  guardrailEvents: event.guardrail_events || [],
                  dimsDegraded: event.dims_degraded || [],
                });
                // done 后把仍在 running/pending 的维度收敛为最终状态
                if (event.dimensions) {
                  setDimStatuses((prev) => {
                    const next = { ...prev };
                    for (const [dimId, payload] of Object.entries(event.dimensions!)) {
                      next[dimId] =
                        payload?.status === 'degraded'
                          ? 'degraded'
                          : payload?.status === 'skipped'
                            ? 'skipped'
                            : 'ok';
                    }
                    return next;
                  });
                }
              }
              if (rid) {
                setStatus('done');
              } else {
                setStatus('error');
                setError(event.message || event.error || '报告生成失败');
              }
              break;
            }

            if (event.type === 'error') {
              doneReceived = true;
              setStatus('error');
              setError(event.message || '生成失败，请重试');
              break;
            }

            if (event.type === 'heartbeat') {
              continue; // 心跳仅用于重置 watchdog，不入 steps
            }

            // 双轨维度徽章事件（dim_start/dim_done）
            if ((event.type === 'dim_start' || event.type === 'dim_done') && event.dim) {
              const dimId = event.dim;
              setDimStatuses((prev) =>
                prev[dimId] === undefined
                  ? prev
                  : {
                      ...prev,
                      [dimId]:
                        event.type === 'dim_start'
                          ? 'running'
                          : event.status === 'degraded'
                            ? 'degraded'
                            : event.status === 'skipped'
                              ? 'skipped'
                              : 'ok',
                    },
              );
            }

            // thinking / tool_start / tool_done / generating / dim_* → 进度步骤
            setProgressSteps((prev) => [...prev, event]);
          }

          if (doneReceived) break;
        }

        // 流正常结束但没收到 done/error 事件
        if (!doneReceived && !ac.signal.aborted) {
          setStatus('error');
          setError('连接已结束但未收到完整报告，请稍后在历史列表查看或重试。');
        }
      } catch (e: unknown) {
        const err = e as { name?: string; message?: string };
        if (err?.name === 'AbortError') {
          // 用户取消：静默回 idle
          setStatus('idle');
        } else {
          setStatus('error');
          setError(err?.message || '连接失败，请检查网络后重试');
        }
      } finally {
        clearWatchdog();
        abortRef.current = null;
      }
    },
    [resetWatchdog, clearWatchdog],
  );

  const cancel = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
    }
    setStatus('idle');
  }, []);

  const reset = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
    }
    clearWatchdog();
    setStatus('idle');
    setProgressSteps([]);
    setReport(null);
    setReportId(null);
    setError(null);
    setDimStatuses(INITIAL_DIM_STATUSES);
    setDualTrack({ guardrailEvents: [], dimsDegraded: [], cacheHit: false });
  }, [clearWatchdog]);

  // 卸载时清理
  useEffect(() => {
    return () => {
      if (abortRef.current) {
        abortRef.current.abort();
      }
      clearWatchdog();
    };
  }, [clearWatchdog]);

  return {
    status,
    progressSteps,
    report,
    reportId,
    error,
    dimStatuses,
    dualTrack,
    generate,
    cancel,
    reset,
  };
}
