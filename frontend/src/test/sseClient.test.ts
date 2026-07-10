import { afterEach, describe, expect, it, vi } from 'vitest';
import { SseParser, parseSseFrame, streamAgentRun, UnauthorizedError } from '../api/sseClient';
import type { RuntimeEvent } from '../api/types';
import { frame, mockFetchStream, streamResponse } from './sseMock';

afterEach(() => vi.unstubAllGlobals());

describe('SseParser', () => {
  it('parses a single frame', () => {
    const parser = new SseParser();
    const events = parser.push(frame({ type: 'runtime_started', sequence: 0 }));
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('runtime_started');
  });

  it('reassembles a frame split across network chunks', () => {
    const parser = new SseParser();
    const whole = frame({ type: 'answer_chunk', sequence: 1, data: { text: 'hello' } });
    const mid = Math.floor(whole.length / 2);
    expect(parser.push(whole.slice(0, mid))).toHaveLength(0); // partial → nothing yet
    const events = parser.push(whole.slice(mid));
    expect(events).toHaveLength(1);
    expect(events[0].data.text).toBe('hello');
  });

  it('emits multiple frames arriving in one chunk, in order', () => {
    const parser = new SseParser();
    const chunk =
      frame({ type: 'answer_chunk', sequence: 1, data: { text: 'a' } }) +
      frame({ type: 'answer_chunk', sequence: 2, data: { text: 'b' } }) +
      frame({ type: 'answer_chunk', sequence: 3, data: { text: 'c' } });
    const events = parser.push(chunk);
    expect(events.map((e) => e.data.text)).toEqual(['a', 'b', 'c']);
    expect(events.map((e) => e.sequence)).toEqual([1, 2, 3]);
  });

  it('skips a malformed frame safely without throwing', () => {
    const parser = new SseParser();
    const events = parser.push('event: answer_chunk\ndata: {not json}\n\n');
    expect(events).toHaveLength(0);
  });

  it('ignores a frame with no data', () => {
    expect(parseSseFrame('event: ping')).toBeNull();
  });
});

describe('streamAgentRun', () => {
  it('includes credentials and posts the request body', async () => {
    const { calls } = mockFetchStream(streamResponse([frame({ type: 'runtime_completed', sequence: 0, data: { runtime_outcome: 'completed' } })]));
    const events: RuntimeEvent[] = [];
    await streamAgentRun({ user_request: 'hi' }, { onEvent: (e) => events.push(e) }, new AbortController().signal);
    expect(calls[0].init.credentials).toBe('include');
    expect(calls[0].url).toContain('/agent/run/stream');
    expect(JSON.parse(calls[0].init.body as string).user_request).toBe('hi');
    expect(events.map((e) => e.type)).toEqual(['runtime_completed']);
  });

  it('delivers events incrementally and calls onDone', async () => {
    mockFetchStream(streamResponse([
      frame({ type: 'answer_started', sequence: 0 }),
      frame({ type: 'answer_chunk', sequence: 1, data: { text: 'Hel' } }),
      frame({ type: 'answer_chunk', sequence: 2, data: { text: 'lo' } }),
      frame({ type: 'runtime_completed', sequence: 3, data: { runtime_outcome: 'completed' } }),
    ]));
    const types: string[] = [];
    const done = vi.fn();
    await streamAgentRun({ user_request: 'x' }, { onEvent: (e) => types.push(e.type), onDone: done }, new AbortController().signal);
    expect(types).toEqual(['answer_started', 'answer_chunk', 'answer_chunk', 'runtime_completed']);
    expect(done).toHaveBeenCalledOnce();
  });

  it('surfaces a 401 as UnauthorizedError', async () => {
    mockFetchStream(streamResponse([], { status: 401 }));
    const onError = vi.fn();
    await streamAgentRun({ user_request: 'x' }, { onEvent: () => {}, onError }, new AbortController().signal);
    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0][0]).toBeInstanceOf(UnauthorizedError);
  });

  it('stops cleanly when the signal is already aborted', async () => {
    mockFetchStream(streamResponse([frame({ type: 'runtime_completed', sequence: 0, data: { runtime_outcome: 'completed' } })]));
    const controller = new AbortController();
    controller.abort();
    const onError = vi.fn();
    const onEvent = vi.fn();
    await streamAgentRun({ user_request: 'x' }, { onEvent, onError }, controller.signal);
    // an aborted stream neither errors nor emits
    expect(onError).not.toHaveBeenCalled();
    expect(onEvent).not.toHaveBeenCalled();
  });
});
