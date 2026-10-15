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
    finish(event: object) {
      controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`));
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
});
