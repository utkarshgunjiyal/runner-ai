import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { useAgentRun } from '../hooks/useAgentRun';
import { frame, streamResponse } from './sseMock';

afterEach(() => vi.unstubAllGlobals());

/** Stream + JSON resume backend, recording every request. */
function mockBackend(streamFrames: string[], resumeBody: unknown) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fn = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    if (url.includes('/agent/run/stream')) {
      if (init.signal?.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' });
      return streamResponse(streamFrames);
    }
    return new Response(JSON.stringify(resumeBody), { status: 200, headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}

// A run that pauses to disambiguate documents (waiting_for_user + select_document).
const DOC_PAUSE_STREAM = [
  frame({ type: 'runtime_started', sequence: 0 }),
  frame({
    type: 'runtime_completed',
    sequence: 1,
    data: {
      runtime_outcome: 'waiting_for_user',
      pending_action: 'select_document',
      pending_reason: 'multiple documents match',
      checkpoint_id: 'ckpt-doc',
      document_candidates: [
        { document_id: 'd1', filename: 'q3.pdf', created_at: '1' },
        { document_id: 'd2', filename: 'q4.pdf', created_at: '2' },
      ],
    },
  }),
];

const COMPLETED_RESUME = {
  run_id: 'run-1',
  thread_id: 't1',
  runtime_outcome: 'completed',
  answer: 'used d1',
  checkpoint_id: null,
  pending_action: null,
  pending_reason: null,
  metadata: {},
};

describe('thread switch / reset', () => {
  it('populates documentCandidates on a select_document pause', async () => {
    mockBackend(DOC_PAUSE_STREAM, COMPLETED_RESUME);
    const { result } = renderHook(() => useAgentRun());
    act(() => result.current.submit('summarize the report', 't1'));
    await waitFor(() => expect(result.current.state.status).toBe('waiting_for_user'));
    expect(result.current.state.pendingAction).toBe('select_document');
    expect(result.current.state.documentCandidates.map((c) => c.document_id)).toEqual(['d1', 'd2']);
    expect(result.current.state.checkpointId).toBe('ckpt-doc');
  });

  it('reset clears run, checkpoint, and documentCandidates (no leakage across threads)', async () => {
    mockBackend(DOC_PAUSE_STREAM, COMPLETED_RESUME);
    const { result } = renderHook(() => useAgentRun());
    act(() => result.current.submit('x', 't1'));
    await waitFor(() => expect(result.current.state.documentCandidates).toHaveLength(2));

    act(() => result.current.reset());

    expect(result.current.state.status).toBe('idle');
    expect(result.current.state.checkpointId).toBeNull();
    expect(result.current.state.documentCandidates).toEqual([]);
    expect(result.current.state.request).toBeNull();
    expect(result.current.state.answerRounds).toEqual([]);
  });

  it('resumeWithDocuments resumes with the selected ids as a clarification', async () => {
    const { calls } = mockBackend(DOC_PAUSE_STREAM, COMPLETED_RESUME);
    const { result } = renderHook(() => useAgentRun());
    act(() => result.current.submit('x', 't1'));
    await waitFor(() => expect(result.current.state.status).toBe('waiting_for_user'));

    act(() => result.current.resumeWithDocuments(['d1']));
    await waitFor(() => expect(result.current.state.status).toBe('completed'));

    const resumeCall = calls.find((c) => c.url.includes('/agent/resume'))!;
    const payload = JSON.parse(resumeCall.init.body as string);
    expect(payload).toEqual({
      checkpoint_id: 'ckpt-doc',
      resolution: { kind: 'clarification', value: ['d1'] },
    });
  });

  it('sends selected_document_ids and explicit_context_mode in the stream body', async () => {
    const { calls } = mockBackend(DOC_PAUSE_STREAM, COMPLETED_RESUME);
    const { result } = renderHook(() => useAgentRun());
    act(() => result.current.submit('x', 't1', ['d1', 'd2']));
    await waitFor(() => expect(result.current.state.status).toBe('waiting_for_user'));

    const streamCall = calls.find((c) => c.url.includes('/agent/run/stream'))!;
    const body = JSON.parse(streamCall.init.body as string);
    expect(body.thread_id).toBe('t1');
    expect(body.selected_document_ids).toEqual(['d1', 'd2']);
    expect(body.explicit_context_mode).toBe('selected');
  });
});
