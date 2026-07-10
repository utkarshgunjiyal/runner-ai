import type { RunState } from '../../state/runTypes';
import { StreamingMessage } from './StreamingMessage';

/** The conversation area: the user's request + the streaming assistant answer. */
export function MessageList({ state }: { state: RunState }) {
  const streaming = state.status === 'streaming_answer';
  return (
    <div className="messages" data-testid="message-list">
      {state.request ? (
        <div className="msg msg-user" data-testid="user-message">
          <p>{state.request}</p>
        </div>
      ) : (
        <div className="messages-empty">
          <p>Ask Runner.ai to search documents, summarize, or run a multi-step task.</p>
        </div>
      )}
      <StreamingMessage rounds={state.answerRounds} streaming={streaming} />
    </div>
  );
}
