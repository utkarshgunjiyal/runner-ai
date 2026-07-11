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

/** How the run should scope document context. */
export type ExplicitContextMode = 'none' | 'all' | 'selected';

export interface AgentRunRequest {
  user_request: string;
  thread_id?: string | null;
  metadata?: Record<string, JsonValue> | null;
  selected_document_ids?: string[];
  selected_page_numbers?: number[];
  explicit_context_mode?: ExplicitContextMode;
}

/** A thread as returned by GET /threads and POST /threads. */
export interface ThreadSummary {
  thread_id: string;
  title: string;
  updated_at: string;
  created_at?: string;
  message_count: number;
}

/** A persisted message in a thread (GET /threads/{id}/messages). */
export interface ThreadMessage {
  seq: number;
  role: string;
  content: string;
  created_at: string;
}

/** A document attached to a thread (GET /threads/{id}/documents). */
export interface ThreadDocument {
  document_id: string;
  filename: string;
  status: string;
  page_count: number;
  created_at: string;
}

/** A candidate document offered when a run pauses to disambiguate. */
export interface DocumentCandidate {
  document_id: string;
  filename: string;
  created_at: string;
}

/** 202 response from POST /documents/upload. */
export interface DocumentUploadResult {
  document_id: string;
  job_id: string;
  status: string;
}

/** GET /documents/{id} — carries at least a status; other fields optional. */
export interface DocumentStatusResult {
  status: string;
  document_id?: string;
  filename?: string;
  page_count?: number;
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
