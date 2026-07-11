import type { ThreadMessage } from '../../api/types';
import type { RunState } from '../../state/runTypes';
import { StreamingMessage } from './StreamingMessage';

/**
 * The conversation area: the active thread's persisted history, then the current
 * run's request + streaming assistant answer.
 */
export function MessageList({
  state,
  history = [],
}: {
  state: RunState;
  history?: ThreadMessage[];
}) {
  const streaming = state.status === 'streaming_answer';
  const isEmpty = history.length === 0 && !state.request;

  return (
    <div className="messages" data-testid="message-list">
      {history.map((message) => (
        <div
          key={message.seq}
          className={`msg ${message.role === 'user' ? 'msg-user' : 'msg-assistant'}`}
          data-testid="history-message"
          data-role={message.role}
        >
          <p>{message.content}</p>
        </div>
      ))}

      {isEmpty ? (
        <div className="messages-empty">
          <p>Ask Runner.ai to search documents, summarize, or run a multi-step task.</p>
        </div>
      ) : null}

      {state.request ? (
        <div className="msg msg-user" data-testid="user-message">
          <p>{state.request}</p>
        </div>
      ) : null}

      <StreamingMessage rounds={state.answerRounds} streaming={streaming} />
    </div>
  );
}
