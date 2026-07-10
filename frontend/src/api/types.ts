// Typed contracts mirroring the backend exactly (Phase 41B).
// No `any` in core API types.

export type RuntimeOutcome =
  | 'completed'
  | 'completed_with_warning'
  | 'failed'
  | 'waiting_for_context'
  | 'waiting_for_user'
  | 'waiting_for_approval'
  | 'waiting_for_replan';

export type RuntimeEventType =
  | 'runtime_started'
  | 'context_started'
  | 'context_completed'
  | 'retrieval_started'
  | 'retrieval_completed'
  | 'planner_started'
  | 'planner_completed'
  | 'tool_started'
  | 'tool_completed'
  | 'evaluation_started'
  | 'evaluation_completed'
  | 'repair_started'
  | 'repair_completed'
  | 'answer_started'
  | 'answer_chunk'
  | 'answer_completed'
  | 'runtime_completed'
  | 'runtime_failed';

/** Value types allowed inside a RuntimeEvent `data` payload (JSON-safe). */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/** One event off POST /agent/run/stream. `data` carries API-safe fields only. */
export interface RuntimeEvent {
  type: RuntimeEventType;
  sequence: number;
  run_id: string | null;
  data: Record<string, JsonValue>;
}

export interface AgentRunRequest {
  user_request: string;
  thread_id?: string | null;
  metadata?: Record<string, JsonValue> | null;
}

/** POST /agent/resume response (also the shape /agent/run returns). */
export interface AgentRunResponse {
  run_id: string;
  thread_id: string | null;
  runtime_outcome: RuntimeOutcome;
  answer: string | null;
  checkpoint_id: string | null;
  pending_action: string | null;
  pending_reason: string | null;
  metadata: Record<string, JsonValue>;
}

export type ResumeKind =
  | 'approval'
  | 'rejection'
  | 'clarification'
  | 'context_available'
  | 'replan_requested';

export interface ResumeResolution {
  kind: ResumeKind;
  value?: JsonValue;
  reason?: string;
  metadata?: Record<string, JsonValue>;
}

export interface AgentResumeRequest {
  checkpoint_id: string;
  resolution: ResumeResolution;
}

/** The waiting outcomes that pause a run for a human. */
export const WAITING_OUTCOMES: readonly RuntimeOutcome[] = [
  'waiting_for_user',
  'waiting_for_approval',
  'waiting_for_context',
  'waiting_for_replan',
];

export function isWaitingOutcome(outcome: RuntimeOutcome): boolean {
  return WAITING_OUTCOMES.includes(outcome);
}
