// useRuntimeStream (Phase 41B): manages a single abortable SSE stream. Starting a
// new stream aborts any previous one; unmount aborts. Ensures only ONE active
// stream at a time.

import { useCallback, useEffect, useRef } from 'react';
import { streamAgentRun, type StreamCallbacks, type StreamRequestBody } from '../api/sseClient';

export interface RuntimeStreamApi {
  start: (body: StreamRequestBody, callbacks: StreamCallbacks) => void;
  stop: () => void;
}

export function useRuntimeStream(baseUrl = ''): RuntimeStreamApi {
  const controllerRef = useRef<AbortController | null>(null);

  const stop = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
  }, []);

  const start = useCallback(
    (body: StreamRequestBody, callbacks: StreamCallbacks) => {
      // Abort any in-flight stream before starting a new one.
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      void streamAgentRun(
        body,
        {
          onEvent: callbacks.onEvent,
          onError: (error) => {
            if (controllerRef.current === controller) controllerRef.current = null;
            callbacks.onError?.(error);
          },
          onDone: () => {
            if (controllerRef.current === controller) controllerRef.current = null;
            callbacks.onDone?.();
          },
        },
        controller.signal,
        baseUrl,
      );
    },
    [baseUrl],
  );

  // Abort on unmount.
  useEffect(() => () => stop(), [stop]);

  return { start, stop };
}
