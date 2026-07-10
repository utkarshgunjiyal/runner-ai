// useAgentRun (Phase 41B): the top-level run orchestrator. Owns the state machine
// and drives submit → stream, and HITL resolution → resume, mapping backend
// results into safe state transitions. Prevents invalid actions (double stream,
// duplicate resume, resume without checkpoint).

import { useCallback, useReducer, useRef } from 'react';
import { resumeAgentRun, ApiError, ConflictError } from '../api/agentClient';
import { UnauthorizedError } from '../api/sseClient';
import type { ResumeResolution } from '../api/types';
import { runReducer } from '../state/runReducer';
import { canSubmit, initialRunState, type RunState } from '../state/runTypes';
import { useRuntimeStream } from './useRuntimeStream';

export interface AgentRunApi {
  state: RunState;
  submit: (request: string) => void;
  cancel: () => void;
  resume: (resolution: ResumeResolution) => void;
  reset: () => void;
}

function safeStreamError(error: unknown) {
  if (error instanceof UnauthorizedError) {
    return { message: 'Your session has expired. Please sign in again.', sessionExpired: true };
  }
  return { message: 'The connection was interrupted. You can retry.', retryable: true };
}

export function useAgentRun(baseUrl = ''): AgentRunApi {
  const [state, dispatch] = useReducer(runReducer, initialRunState);
  const stream = useRuntimeStream(baseUrl);
  // Synchronous guard: prevents a duplicate resume even when two calls fire in
  // the same render tick (React batches state, so `state.resuming` would be stale).
  const resumingRef = useRef(false);

  const submit = useCallback(
    (request: string) => {
      const trimmed = request.trim();
      if (!trimmed || !canSubmit(state.status)) return;
      dispatch({ type: 'SUBMIT', request: trimmed, threadId: state.threadId });
      stream.start(
        { user_request: trimmed, thread_id: state.threadId },
        {
          onEvent: (event) => dispatch({ type: 'RUNTIME_EVENT', event }),
          onError: (error) => dispatch({ type: 'STREAM_ERROR', error: safeStreamError(error) }),
          onDone: () => dispatch({ type: 'STREAM_DONE' }),
        },
      );
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

  const reset = useCallback(() => {
    stream.stop();
    dispatch({ type: 'RESET' });
  }, [stream]);

  return { state, submit, cancel, resume, reset };
}

type ResolutionInput = ResumeResolution;
