// Run lifecycle reducer (Phase 41B). Pure function: RuntimeEvents + user actions
// drive transitions. Only safe metadata is folded into the timeline; the reducer
// never interprets partial answer chunks and never stores internal runtime state.

import type { AgentRunResponse, DocumentCandidate, JsonValue, RuntimeEvent, RuntimeOutcome } from '../api/types';
import {
  initialRunState,
  statusForOutcome,
  type AnswerRound,
  type RunAction,
  type RunState,
  type SafeError,
  type TimelineItem,
} from './runTypes';

function str(value: JsonValue | undefined): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function numeric(value: JsonValue | undefined): number | undefined {
  return typeof value === 'number' ? value : undefined;
}

/** Parse a `document_candidates` payload into safe DocumentCandidate entries. */
export function parseDocumentCandidates(value: JsonValue | undefined): DocumentCandidate[] {
  if (!Array.isArray(value)) return [];
  const result: DocumentCandidate[] = [];
  for (const entry of value) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
    const documentId = entry.document_id;
    const filename = entry.filename;
    if (typeof documentId === 'string' && typeof filename === 'string') {
      const createdAt = entry.created_at;
      result.push({
        document_id: documentId,
        filename,
        created_at: typeof createdAt === 'string' ? createdAt : '',
      });
    }
  }
  return result;
}

/** Map a runtime event to a safe timeline entry, or null to skip it. */
export function toTimelineItem(event: RuntimeEvent): TimelineItem | null {
  const key = `${event.sequence}`;
  const d = event.data;
  switch (event.type) {
    case 'context_completed': {
      const size = numeric(d.context_size);
      return { key, kind: 'context', label: 'Context assembled', status: 'ok', detail: size != null ? `${size} items` : undefined };
    }
    case 'retrieval_completed': {
      const caps = Array.isArray(d.selected_capabilities) ? d.selected_capabilities.length : undefined;
      return { key, kind: 'retrieval', label: 'Capabilities retrieved', status: 'ok', detail: caps != null ? `${caps} selected` : undefined };
    }
    case 'planner_completed':
      return { key, kind: 'planner', label: 'Planner created a plan', status: 'ok', detail: str(d.runtime_status) };
    case 'tool_started':
      return { key, kind: 'tool', label: `Tool started`, status: 'info', detail: str(d.capability_id) };
    case 'tool_completed':
      return { key, kind: 'tool', label: `Tool completed`, status: 'ok', detail: str(d.capability_id) };
    case 'evaluation_completed': {
      const passed = d.passed === true;
      return { key, kind: 'evaluation', label: passed ? 'Evaluation passed' : 'Evaluation flagged issues', status: passed ? 'ok' : 'warn' };
    }
    case 'repair_started':
      return { key, kind: 'repair', label: 'Repair started', status: 'info', detail: str(d.action) };
    case 'repair_completed':
      return { key, kind: 'repair', label: 'Repair completed', status: 'ok', detail: str(d.action) };
    default:
      return null; // started/answer_*/terminal events are shown elsewhere
  }
}

/** Extract a safe error from a runtime_failed / stream error (no raw text). */
export function safeErrorFromFailedEvent(event: RuntimeEvent): SafeError {
  const d = event.data;
  const reason = str(d.reason) || str(d.pending_reason);
  const code = str(d.error_code) || str(d.failure_stage);
  const retryable = d.retryable === true;
  return {
    message: reason || 'The run could not be completed. Please try again.',
    code,
    retryable,
  };
}

function appendChunk(rounds: AnswerRound[], chunk: string): AnswerRound[] {
  if (rounds.length === 0) return [{ text: chunk, completed: false }];
  const next = rounds.slice();
  const last = next[next.length - 1];
  next[next.length - 1] = { ...last, text: last.text + chunk };
  return next;
}

export function runReducer(state: RunState, action: RunAction): RunState {
  switch (action.type) {
    case 'SUBMIT':
      return {
        ...initialRunState,
        status: 'connecting',
        request: action.request,
        threadId: action.threadId,
      };

    case 'RUNTIME_EVENT':
      return applyEvent(state, action.event);

    case 'STREAM_ERROR':
      return { ...state, status: 'failed', error: action.error, resuming: false };

    case 'STREAM_DONE':
      // The terminal event already set a definitive status; if the stream ended
      // without one (unexpected), fall back to failed.
      if (
        state.status === 'connecting' ||
        state.status === 'running' ||
        state.status === 'streaming_answer'
      ) {
        return { ...state, status: 'failed', error: { message: 'The stream ended unexpectedly.' } };
      }
      return state;

    case 'CANCEL':
      return { ...state, status: 'cancelled' };

    case 'RESUME_START':
      return { ...state, resuming: true, error: null };

    case 'RESUME_RESULT':
      return applyResumeResponse(state, action.response);

    case 'RESUME_ERROR':
      return {
        ...state,
        resuming: false,
        error: action.error,
        checkpointId: action.clearCheckpoint ? null : state.checkpointId,
      };

    case 'RESET':
      return initialRunState;

    default:
      return state;
  }
}

function applyEvent(state: RunState, event: RuntimeEvent): RunState {
  const d = event.data;
  switch (event.type) {
    case 'runtime_started':
      return { ...state, status: 'running', runId: event.run_id, timeline: [], answerRounds: [] };

    case 'answer_started':
      // A fresh round (also covers a second, bounded repair regeneration round).
      return {
        ...state,
        status: 'streaming_answer',
        answerRounds: [...state.answerRounds, { text: '', completed: false }],
      };

    case 'answer_chunk': {
      const chunk = str(d.text) ?? '';
      return { ...state, status: 'streaming_answer', answerRounds: appendChunk(state.answerRounds, chunk) };
    }

    case 'answer_completed': {
      // Finalize the active round with the authoritative completed text.
      const text = str(d.text) ?? currentRoundText(state);
      const rounds = state.answerRounds.length > 0 ? state.answerRounds.slice() : [{ text: '', completed: false }];
      rounds[rounds.length - 1] = { text, completed: true };
      return { ...state, answerRounds: rounds, runId: event.run_id ?? state.runId };
    }

    case 'runtime_completed': {
      const outcome = (str(d.runtime_outcome) as RuntimeOutcome) ?? 'completed';
      return {
        ...state,
        status: statusForOutcome(outcome),
        outcome,
        runId: event.run_id ?? state.runId,
        threadId: str(d.thread_id) ?? state.threadId,
        checkpointId: str(d.checkpoint_id) ?? null,
        pendingAction: str(d.pending_action) ?? null,
        pendingReason: str(d.pending_reason) ?? null,
        documentCandidates: parseDocumentCandidates(d.document_candidates),
      };
    }

    case 'runtime_failed':
      return { ...state, status: 'failed', outcome: 'failed', error: safeErrorFromFailedEvent(event) };

    default: {
      const item = toTimelineItem(event);
      return item ? { ...state, timeline: [...state.timeline, item] } : state;
    }
  }
}

function applyResumeResponse(state: RunState, response: AgentRunResponse): RunState {
  const status = statusForOutcome(response.runtime_outcome);
  const answerRounds =
    response.answer != null
      ? [...state.answerRounds, { text: response.answer, completed: true }]
      : state.answerRounds;
  return {
    ...state,
    resuming: false,
    status,
    outcome: response.runtime_outcome,
    runId: response.run_id ?? state.runId,
    threadId: response.thread_id ?? state.threadId,
    answerRounds,
    // A new checkpoint if it waits again; cleared once completed/failed.
    checkpointId: response.checkpoint_id,
    pendingAction: response.pending_action,
    pendingReason: response.pending_reason,
    documentCandidates: parseDocumentCandidates(response.metadata?.document_candidates),
  };
}

function currentRoundText(state: RunState): string {
  const round = state.answerRounds[state.answerRounds.length - 1];
  return round ? round.text : '';
}
