import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { generateStream, cancelRun } = vi.hoisted(() => ({ generateStream: vi.fn(), cancelRun: vi.fn() }));
vi.mock('../../api/deepResearch', () => ({ deepResearchApi: { generateStream, cancelRun } }));
import { useDeepResearch } from '../useDeepResearch';

function stream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  return {
    response: new Response(body),
    send(event: object) {
      controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`));
    },
    finish(event: object) {
      this.send(event);
      controller.close();
    },
  };
}

describe('deep research run lifecycle', () => {
  beforeEach(() => { generateStream.mockReset(); cancelRun.mockReset(); });
  afterEach(cleanup);

  it('queues early cancellation until acceptance and waits for actual terminal state', async () => {
    const pending = stream();
    let accept!: (value: Response) => void;
    generateStream.mockReturnValue(new Promise<Response>((resolve) => { accept = resolve; }));
    cancelRun.mockResolvedValue({ status: 'cancelling' });
    const { result } = renderHook(() => useDeepResearch());
    let generation!: Promise<void>;
    act(() => { generation = result.current.generate('300260', '样本'); });
    act(() => { result.current.cancel(); });
    expect(result.current.status).toBe('cancelling');
    expect(cancelRun).not.toHaveBeenCalled();
    await act(async () => { accept(pending.response); });
    await waitFor(() => expect(cancelRun).toHaveBeenCalledWith(generateStream.mock.calls[0][0].run_id));
    expect(generateStream.mock.calls[0][1].signal.aborted).toBe(false);
    expect(result.current.status).toBe('cancelling');
    await act(async () => {
      pending.finish({ type: 'done', end_reason: 'cancelled', report_id: null, status: 'failed', markdown: '' });
      await generation;
    });
    expect(result.current.status).toBe('cancelled');
    expect(result.current.error).toBeNull();
  });

  it('unmount only detaches the stream and never sends a cancellation', async () => {
    const pending = stream();
    generateStream.mockResolvedValue(pending.response);
    const { result, unmount } = renderHook(() => useDeepResearch());
    let generation!: Promise<void>;
    await act(async () => { generation = result.current.generate('300260'); });
    const signal = generateStream.mock.calls[0][1].signal as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    expect(cancelRun).not.toHaveBeenCalled();
    pending.finish({ type: 'done', report_id: '300260_202610151200', markdown: '# saved' });
    await generation;
  });

  it('tracks only the current 18 active dims and preserves availability through done/reset', async () => {
    const pending = stream();
    generateStream.mockResolvedValue(pending.response);
    const { result } = renderHook(() => useDeepResearch());
    // 新运行初始进度 = 当前 18 项，无退役 ownership 占位
    expect(Object.keys(result.current.dimStatuses)).toHaveLength(18);
    expect(result.current.dimStatuses.ownership).toBeUndefined();

    let generation!: Promise<void>;
    await act(async () => { generation = result.current.generate('300260'); });
    await act(async () => {
      // 旧客户端兼容广播：退役维度终态不进入新客户端进度（徽章与步骤均不出现）
      pending.send({ type: 'dim_done', dim: 'ownership', status: 'skipped' });
      pending.send({ type: 'dim_done', dim: 'fundamental', status: 'ok' });
    });
    await waitFor(() => expect(result.current.dimStatuses.fundamental).toBe('ok'));
    expect(result.current.dimStatuses.ownership).toBeUndefined();
    expect(result.current.progressSteps.some((s) => s.dim === 'ownership')).toBe(false);
    expect(result.current.progressSteps.some((s) => s.dim === 'fundamental')).toBe(true);

    await act(async () => {
      pending.finish({
        type: 'done',
        report_id: '300260_202610291200',
        status: 'completed',
        markdown: '# ok',
        engine: 'dual_track',
        dimensions: { fundamental: { status: 'ok' }, ownership: { status: 'skipped' } },
        available_dim_reports: ['fundamental', 'ownership'],
      });
      await generation;
    });
    expect(result.current.status).toBe('done');
    // done 事件可选字段传播到 report 状态；退役维度收敛不回填当前状态
    expect(result.current.report?.available_dim_reports).toEqual(['fundamental', 'ownership']);
    expect(result.current.dimStatuses.ownership).toBeUndefined();

    act(() => { result.current.reset(); });
    expect(result.current.report).toBeNull();
    expect(Object.keys(result.current.dimStatuses)).toHaveLength(18);
  });

  it('reads legacy done payloads without the optional availability field', async () => {
    const pending = stream();
    generateStream.mockResolvedValue(pending.response);
    const { result } = renderHook(() => useDeepResearch());
    let generation!: Promise<void>;
    await act(async () => { generation = result.current.generate('300260'); });
    await act(async () => {
      pending.finish({ type: 'done', report_id: '300260_202610291200', markdown: '# legacy' });
      await generation;
    });
    expect(result.current.status).toBe('done');
    expect(result.current.report?.available_dim_reports).toBeUndefined();
  });
});
