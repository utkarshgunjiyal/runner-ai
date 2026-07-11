import type { ThreadSummary } from '../../api/types';

/** Left rail: the conversation list + a "New conversation" action. Functional. */
export function ThreadSidebar({
  threads,
  activeThreadId,
  onSelect,
  onNew,
}: {
  threads: ThreadSummary[];
  activeThreadId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  return (
    <aside className="thread-sidebar" data-testid="thread-sidebar">
      <button
        type="button"
        className="btn btn-primary thread-new"
        onClick={onNew}
        data-testid="new-thread-btn"
      >
        New conversation
      </button>
      <ul className="thread-list">
        {threads.map((thread) => {
          const active = thread.thread_id === activeThreadId;
          return (
            <li key={thread.thread_id}>
              <button
                type="button"
                className={`thread-item${active ? ' active' : ''}`}
                onClick={() => onSelect(thread.thread_id)}
                data-testid="thread-item"
                aria-current={active ? 'true' : undefined}
              >
                <span className="thread-title">{thread.title || 'Untitled conversation'}</span>
                <span className="thread-count">{thread.message_count}</span>
              </button>
            </li>
          );
        })}
        {threads.length === 0 ? (
          <li className="thread-empty">No conversations yet.</li>
        ) : null}
      </ul>
    </aside>
  );
}
