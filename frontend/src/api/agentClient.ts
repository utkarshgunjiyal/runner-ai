// JSON API client for the non-streaming endpoints (Phase 41B).
// Resume is JSON (not SSE) — we never pretend it token-streams.

import type { AgentResumeRequest, AgentRunResponse } from './types';
import { UnauthorizedError } from './sseClient';

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export class ConflictError extends ApiError {
  constructor(message = 'checkpoint conflict') {
    super(409, message);
    this.name = 'ConflictError';
  }
}

/** POST /agent/resume — continue a paused run. Cookies included. */
export async function resumeAgentRun(
  request: AgentResumeRequest,
  baseUrl = '',
): Promise<AgentRunResponse> {
  const response = await fetch(`${baseUrl}/agent/resume`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(request),
    credentials: 'include',
  });

  if (response.status === 401) throw new UnauthorizedError();
  if (response.status === 404) throw new ApiError(404, 'checkpoint not found');
  if (response.status === 409) throw new ConflictError();
  if (!response.ok) throw new ApiError(response.status, `resume failed: HTTP ${response.status}`);

  return (await response.json()) as AgentRunResponse;
}
