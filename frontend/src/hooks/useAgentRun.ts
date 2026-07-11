// useAgentRun (Phase 41B): the top-level run orchestrator. Owns the state machine
// and drives submit → stream, and HITL resolution → resume, mapping backend
// results into safe state transitions. Prevents invalid actions (double stream,
// duplicate resume, resume without checkpoint).

import { useCallback, useReducer, useRef } from 'react';
import { resumeAgentRun, ApiError, ConflictError } from '../api/agentClient';
import { UnauthorizedError, type StreamRequestBody } from '../api/sseClient';
import type { ResumeResolution } from '../api/types';
import { runReducer } from '../state/runReducer';
import { canSubmit, initialRunState, type RunState } from '../state/runTypes';
import { useRuntimeStream } from './useRuntimeStream';

export interface AgentRunApi {
  state: RunState;
  submit: (request: string, threadId?: string | null, selectedDocumentIds?: string[]) => void;
  cancel: () => void;
  resume: (resolution: ResumeResolution) => void;
  resumeWithDocuments: (documentIds: string[]) => void;
  reset: () => void;
}

function safeStreamError(error: unknown) {
  if (error instanceof UnauthorizedError) {
    return { message: 'Your session has expired. Please sign in again.', sessionExpired: true };
  }
  return { message: 'The connection was interrupted. You can retry.', retryable: true };
}

export function useAgentRun(
  baseUrl = '',
  onThreadCreated?: (threadId: string) => void,
): AgentRunApi {
  const [state, dispatch] = useReducer(runReducer, initialRunState);
  const stream = useRuntimeStream(baseUrl);
  // Synchronous guard: prevents a duplicate resume even when two calls fire in
  // the same render tick (React batches state, so `state.resuming` would be stale).
  const resumingRef = useRef(false);
  // Keep the latest thread-created callback without re-creating `submit`.
  const onThreadCreatedRef = useRef(onThreadCreated);
  onThreadCreatedRef.current = onThreadCreated;

  const submit = useCallback(
    (request: string, threadId?: string | null, selectedDocumentIds?: string[]) => {
      const trimmed = request.trim();
      if (!trimmed || !canSubmit(state.status)) return;
      const resolvedThreadId = threadId !== undefined ? threadId : state.threadId;
      dispatch({ type: 'SUBMIT', request: trimmed, threadId: resolvedThreadId });
      const body: StreamRequestBody = { user_request: trimmed, thread_id: resolvedThreadId };
      if (selectedDocumentIds && selectedDocumentIds.length > 0) {
        body.selected_document_ids = selectedDocumentIds;
        body.explicit_context_mode = 'selected';
      }
      stream.start(body, {
        onEvent: (event) => {
          dispatch({ type: 'RUNTIME_EVENT', event });
          // The first message in a new conversation auto-creates a thread server
          // side; surface the id so the sidebar can refresh + track it.
          const tid = event.data.thread_id;
          if (typeof tid === 'string' && tid && tid !== resolvedThreadId) {
            onThreadCreatedRef.current?.(tid);
          }
        },
        onError: (error) => dispatch({ type: 'STREAM_ERROR', error: safeStreamError(error) }),
        onDone: () => dispatch({ type: 'STREAM_DONE' }),
      });
    },
    [state.status, state.threadId, stream],
  );

  const cancel = useCallback(() => {
    stream.stop();
    dispatch({ type: 'CANCEL' });
  }, [stream]);

  const resume = useCallback(
    (resolution: ResolutionInput) => {
      // Guards: need a checkpoint, and never resume twice concurrently.
      if (!state.checkpointId || resumingRef.current) return;
      resumingRef.current = true;
      const checkpointId = state.checkpointId;
      dispatch({ type: 'RESUME_START' });
      void (async () => {
        try {
          const response = await resumeAgentRun(
            { checkpoint_id: checkpointId, resolution },
            baseUrl,
          );
          dispatch({ type: 'RESUME_RESULT', response });
        } catch (error) {
          if (error instanceof UnauthorizedError) {
            dispatch({ type: 'RESUME_ERROR', error: { message: 'Your session has expired. Please sign in again.', sessionExpired: true } });
          } else if (error instanceof ConflictError) {
            dispatch({ type: 'RESUME_ERROR', error: { message: 'This step was already resolved.' }, clearCheckpoint: true });
          } else if (error instanceof ApiError && error.status === 404) {
            dispatch({ type: 'RESUME_ERROR', error: { message: 'This run can no longer be resumed.' }, clearCheckpoint: true });
          } else {
            dispatch({ type: 'RESUME_ERROR', error: { message: 'Could not continue the run. Please try again.', retryable: true } });
          }
        } finally {
          resumingRef.current = false;
        }
      })();
    },
    [state.checkpointId, baseUrl],
  );

  const resumeWithDocuments = useCallback(
    (documentIds: string[]) => {
      resume({ kind: 'clarification', value: documentIds });
    },
    [resume],
  );

  const reset = useCallback(() => {
    stream.stop();
    dispatch({ type: 'RESET' });
  }, [stream]);

  return { state, submit, cancel, resume, resumeWithDocuments, reset };
}

type ResolutionInput = ResumeResolution;
