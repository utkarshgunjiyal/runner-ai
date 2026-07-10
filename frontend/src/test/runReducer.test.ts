import { describe, expect, it } from 'vitest';
import { runReducer, toTimelineItem } from '../state/runReducer';
import { currentAnswer, initialRunState, type RunState } from '../state/runTypes';
import type { RuntimeEvent, RuntimeEventType } from '../api/types';

function ev(type: RuntimeEventType, data: Record<string, unknown> = {}, sequence = 0): RuntimeEvent {
  return { type, sequence, run_id: 'run-1', data: data as RuntimeEvent['data'] };
}

function feed(events: RuntimeEvent[], start: RunState = initialRunState): RunState {
  return events.reduce((s, event) => runReducer(s, { type: 'RUNTIME_EVENT', event }), start);
}

describe('streaming answer', () => {
  it('renders chunks incrementally and reconstructs the final text', () => {
    let state = runReducer(initialRunState, { type: 'SUBMIT', request: 'hi', threadId: null });
    state = feed([
      ev('runtime_started'),
      ev('answer_started', {}, 1),
      ev('answer_chunk', { text: 'Hel' }, 2),
    ], state);
    expect(currentAnswer(state)).toBe('Hel');
    expect(state.status).toBe('streaming_answer');
    state = feed([ev('answer_chunk', { text: 'lo' }, 3)], state);
    expect(currentAnswer(state)).toBe('Hello');
    state = feed([ev('answer_completed', { text: 'Hello world' }, 4)], state);
    // authoritative completed text replaces the accumulated chunks
    expect(currentAnswer(state)).toBe('Hello world');
    expect(state.answerRounds[0].completed).toBe(true);
  });

  it('a second answer_started begins a new (repair) round, keeping the prior one', () => {
    const state = feed([
      ev('answer_started', {}, 0),
      ev('answer_chunk', { text: 'first' }, 1),
      ev('answer_completed', { text: 'first' }, 2),
      ev('repair_started', { action: 'regenerate' }, 3),
      ev('answer_started', {}, 4),
      ev('answer_chunk', { text: 'second' }, 5),
    ]);
    expect(state.answerRounds).toHaveLength(2);
    expect(state.answerRounds[0].text).toBe('first');
    expect(currentAnswer(state)).toBe('second');
  });
});

describe('runtime timeline', () => {
  it('shows planner events only when received', () => {
    const withoutPlanner = feed([ev('tool_completed', { capability_id: 'search_documents' })]);
    expect(withoutPlanner.timeline.some((i) => i.kind === 'planner')).toBe(false);
    const withPlanner = feed([ev('planner_completed', { runtime_status: 'ok' })]);
    expect(withPlanner.timeline.some((i) => i.kind === 'planner')).toBe(true);
  });

  it('renders tool + evaluation + repair events with safe fields', () => {
    const state = feed([
      ev('tool_started', { capability_id: 'search_documents' }, 0),
      ev('tool_completed', { capability_id: 'search_documents', output_keys: ['hits'] }, 1),
      ev('evaluation_completed', { passed: true, overall_score: 0.9 }, 2),
      ev('repair_completed', { action: 'regenerate', applied: true }, 3),
    ]);
    const kinds = state.timeline.map((i) => i.kind);
    expect(kinds).toEqual(['tool', 'tool', 'evaluation', 'repair']);
    const toolItem = state.timeline[1];
    expect(toolItem.detail).toBe('search_documents');
  });

  it('does not surface raw/internal fields in timeline items', () => {
    const item = toTimelineItem(
      ev('tool_completed', {
        capability_id: 'search_documents',
        output_keys: ['hits'],
        // hostile extra fields that must never be rendered
        prompt: 'SECRET PROMPT',
        headers: { Authorization: 'token' },
      }),
    );
    const serialized = JSON.stringify(item);
    expect(serialized).not.toContain('SECRET PROMPT');
    expect(serialized).not.toContain('Authorization');
    expect(serialized).not.toContain('output_keys');
  });
});

describe('terminal outcomes', () => {
  it('runtime_completed with a waiting outcome captures the checkpoint id', () => {
    const state = feed([
      ev('runtime_completed', {
        runtime_outcome: 'waiting_for_user',
        pending_action: 'ask_user_for_clarification',
        pending_reason: 'need info',
        checkpoint_id: 'ckpt-1',
      }),
    ]);
    expect(state.status).toBe('waiting_for_user');
    expect(state.checkpointId).toBe('ckpt-1');
    expect(state.pendingReason).toBe('need info');
  });

  it('runtime_failed yields a safe error and no internal text', () => {
    const state = feed([
      ev('runtime_failed', {
        runtime_outcome: 'failed',
        error_code: 'final_provider_error',
        retryable: true,
        reason: 'The final answer could not be generated.',
        error: 'raw vendor stack trace SECRET',
      }),
    ]);
    expect(state.status).toBe('failed');
    expect(state.error?.message).toBe('The final answer could not be generated.');
    expect(state.error?.retryable).toBe(true);
    expect(JSON.stringify(state.error)).not.toContain('SECRET');
  });
});
