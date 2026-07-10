import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { useAgentRun } from '../hooks/useAgentRun';
import { frame, streamResponse } from './sseMock';

afterEach(() => vi.unstubAllGlobals());

/** A fetch mock that streams for /agent/run/stream and returns JSON for /agent/resume. */
function mockBackend(streamFrames: string[], resumeResponses: Array<{ status: number; body: unknown }>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  let resumeIdx = 0;
  const fn = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    if (url.includes('/agent/run/stream')) {
      if (init.signal?.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' });
      return streamResponse(streamFrames);
    }
    const r = resumeResponses[Math.min(resumeIdx++, resumeResponses.length - 1)];
    return new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}

const WAITING_STREAM = [
  frame({ type: 'runtime_started', sequence: 0 }),
  frame({ type: 'answer_started', sequence: 1 }),
  frame({ type: 'answer_chunk', sequence: 2, data: { text: 'partial' } }),
  frame({ type: 'answer_completed', sequence: 3, data: { text: 'partial' } }),
  frame({
    type: 'runtime_completed',
    sequence: 4,
    data: { runtime_outcome: 'waiting_for_user', pending_action: 'ask_user_for_clarification', pending_reason: 'need info', checkpoint_id: 'ckpt-1' },
  }),
];

function completedResponse(answer: string) {
  return { status: 200, body: { run_id: 'run-1', thread_id: null, runtime_outcome: 'completed', answer, checkpoint_id: null, pending_action: null, pending_reason: null, metadata: {} } };
}

describe('useAgentRun — streamed HITL flow', () => {
  it('submits, reaches waiting_for_user with a checkpoint, then resumes to completed', async () => {
    mockBackend(WAITING_STREAM, [completedResponse('final answer')]);
    const { result } = renderHook(() => useAgentRun());

    act(() => result.current.submit('do a thing'));
    await waitFor(() => expect(result.current.state.status).toBe('waiting_for_user'));
    expect(result.current.state.checkpointId).toBe('ckpt-1');

    act(() => result.current.resume({ kind: 'clarification', value: 'the Q3 report' }));
    await waitFor(() => expect(result.current.state.status).toBe('completed'));
    expect(result.current.state.answerRounds.at(-1)?.text).toBe('final answer');
    expect(result.current.state.checkpointId).toBeNull(); // cleared after completion
  });

  it('sends the correct resume payload with credentials', async () => {
    const { calls } = mockBackend(WAITING_STREAM, [completedResponse('ok')]);
    const { result } = renderHook(() => useAgentRun());
    act(() => result.current.submit('x'));
    await waitFor(() => expect(result.current.state.status).toBe('waiting_for_user'));
    act(() => result.current.resume({ kind: 'clarification', value: 'more' }));
    await waitFor(() => expect(result.current.state.status).toBe('completed'));

    const resumeCall = calls.find((c) => c.url.includes('/agent/resume'))!;
    expect(resumeCall.init.credentials).toBe('include');
    const payload = JSON.parse(resumeCall.init.body as string);
    expect(payload).toEqual({ checkpoint_id: 'ckpt-1', resolution: { kind: 'clarification', value: 'more' } });
  });

  it('prevents a duplicate resume (second call is a no-op while resuming)', async () => {
    const { calls } = mockBackend(WAITING_STREAM, [completedResponse('done')]);
    const { result } = renderHook(() => useAgentRun());
    act(() => result.current.submit('x'));
    await waitFor(() => expect(result.current.state.status).toBe('waiting_for_user'));

    act(() => {
      result.current.resume({ kind: 'clarification', value: 'a' });
      result.current.resume({ kind: 'clarification', value: 'b' }); // ignored: already resuming
    });
    await waitFor(() => expect(result.current.state.status).toBe('completed'));
    expect(calls.filter((c) => c.url.includes('/agent/resume'))).toHaveLength(1);
  });

  it('replaces the checkpoint id when the run waits again', async () => {
    mockBackend(WAITING_STREAM, [
      { status: 200, body: { run_id: 'run-1', thread_id: null, runtime_outcome: 'waiting_for_user', answer: null, checkpoint_id: 'ckpt-2', pending_action: 'ask_user_for_clarification', pending_reason: 'still unclear', metadata: {} } },
    ]);
    const { result } = renderHook(() => useAgentRun());
    act(() => result.current.submit('x'));
    await waitFor(() => expect(result.current.state.checkpointId).toBe('ckpt-1'));
    act(() => result.current.resume({ kind: 'clarification', value: 'a' }));
    await waitFor(() => expect(result.current.state.checkpointId).toBe('ckpt-2'));
    expect(result.current.state.status).toBe('waiting_for_user');
  });

  it('does not resume without a checkpoint', async () => {
    const { calls } = mockBackend(
      [frame({ type: 'runtime_completed', sequence: 0, data: { runtime_outcome: 'completed' } })],
      [completedResponse('x')],
    );
    const { result } = renderHook(() => useAgentRun());
    act(() => result.current.submit('x'));
    await waitFor(() => expect(result.current.state.status).toBe('completed'));
    act(() => result.current.resume({ kind: 'clarification', value: 'a' })); // no checkpoint → ignored
    expect(calls.some((c) => c.url.includes('/agent/resume'))).toBe(false);
  });

  it('handles a 401 on resume as a session-expired state', async () => {
    mockBackend(WAITING_STREAM, [{ status: 401, body: {} }]);
    const { result } = renderHook(() => useAgentRun());
    act(() => result.current.submit('x'));
    await waitFor(() => expect(result.current.state.status).toBe('waiting_for_user'));
    act(() => result.current.resume({ kind: 'clarification', value: 'a' }));
    await waitFor(() => expect(result.current.state.error?.sessionExpired).toBe(true));
    expect(result.current.state.resuming).toBe(false);
  });
});
