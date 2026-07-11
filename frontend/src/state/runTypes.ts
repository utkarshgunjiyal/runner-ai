// Run lifecycle state model (Phase 41B). An explicit state machine, not a bag
// of booleans — events drive transitions and invalid actions are prevented.

import type { AgentRunResponse, DocumentCandidate, RuntimeEvent, RuntimeOutcome } from '../api/types';

export type RunStatus =
  | 'idle'
  | 'connecting'
  | 'running'
  | 'streaming_answer'
  | 'waiting_for_user'
  | 'waiting_for_approval'
  | 'waiting_for_context'
  | 'waiting_for_replan'
  | 'completed'
  | 'failed'
  | 'cancelled';

/** A curated, safe timeline entry (never raw prompts/state/secrets). */
export interface TimelineItem {
  key: string;
  kind: string;
  label: string;
  detail?: string;
  status?: 'ok' | 'warn' | 'error' | 'info';
}

/** One answer generation round (a bounded repair produces a second round). */
export interface AnswerRound {
  text: string;
  completed: boolean;
}

/** A safe, display-ready error (no raw provider/transport text). */
export interface SafeError {
  message: string;
  code?: string;
  retryable?: boolean;
  sessionExpired?: boolean;
}

export interface RunState {
  status: RunStatus;
  request: string | null;
  runId: string | null;
  threadId: string | null;
  checkpointId: string | null;
  timeline: TimelineItem[];
  answerRounds: AnswerRound[];
  outcome: RuntimeOutcome | null;
  pendingAction: string | null;
  pendingReason: string | null;
  error: SafeError | null;
  resuming: boolean;
  documentCandidates: DocumentCandidate[];
}

export type RunAction =
  | { type: 'SUBMIT'; request: string; threadId: string | null }
  | { type: 'RUNTIME_EVENT'; event: RuntimeEvent }
  | { type: 'STREAM_ERROR'; error: SafeError }
  | { type: 'STREAM_DONE' }
  | { type: 'CANCEL' }
  | { type: 'RESUME_START' }
  | { type: 'RESUME_RESULT'; response: AgentRunResponse }
  | { type: 'RESUME_ERROR'; error: SafeError; clearCheckpoint?: boolean }
  | { type: 'RESET' };

export const initialRunState: RunState = {
  status: 'idle',
  request: null,
  runId: null,
  threadId: null,
  checkpointId: null,
  timeline: [],
  answerRounds: [],
  outcome: null,
  pendingAction: null,
  pendingReason: null,
  error: null,
  resuming: false,
  documentCandidates: [],
};

/** The waiting statuses map 1:1 to the waiting outcomes. */
export function statusForOutcome(outcome: RuntimeOutcome): RunStatus {
  switch (outcome) {
    case 'waiting_for_user':
      return 'waiting_for_user';
    case 'waiting_for_approval':
      return 'waiting_for_approval';
    case 'waiting_for_context':
      return 'waiting_for_context';
    case 'waiting_for_replan':
      return 'waiting_for_replan';
    case 'failed':
      return 'failed';
    default:
      return 'completed';
  }
}

export function isWaitingStatus(status: RunStatus): boolean {
  return (
    status === 'waiting_for_user' ||
    status === 'waiting_for_approval' ||
    status === 'waiting_for_context' ||
    status === 'waiting_for_replan'
  );
}

/** Can the user submit a brand-new request right now? */
export function canSubmit(status: RunStatus): boolean {
  return (
    status === 'idle' ||
    status === 'completed' ||
    status === 'failed' ||
    status === 'cancelled'
  );
}

/** Is a stream currently active (so we must not start a second one)? */
export function isStreamActive(status: RunStatus): boolean {
  return status === 'connecting' || status === 'running' || status === 'streaming_answer';
}

/** The finalized/active answer text to display (last round wins). */
export function currentAnswer(state: RunState): string {
  const round = state.answerRounds[state.answerRounds.length - 1];
  return round ? round.text : '';
}
