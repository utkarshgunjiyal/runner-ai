import type { ThreadSummary } from '../../api/types';
import { relativeTime } from '../../lib/format';
import { IntegrationsPanel } from '../integrations/IntegrationsPanel';

/**
 * Left rail: branding, a "New conversation" action, the conversation list (with
 * relative last-activity time + message count, loading skeleton, empty and
 * error+retry states), and a truthful integrations section. Never shows raw
 * thread ids. Keyboard accessible (native buttons + aria-current).
 */
export function ThreadSidebar({
  threads,
  activeThreadId,
  loading = false,
  error = false,
  baseUrl = '',
  onSelect,
  onNew,
  onRetry,
}: {
  threads: ThreadSummary[];
  activeThreadId: string | null;
  loading?: boolean;
  error?: boolean;
  baseUrl?: string;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRetry?: () => void;
}) {
  const showSkeleton = loading && threads.length === 0 && !error;

  return (
    <aside className="thread-sidebar" data-testid="thread-sidebar" aria-label="Conversations">
      <div className="sidebar-brand">
        <span className="brand-mark" aria-hidden>
          ◇
        </span>
        Runner.ai
      </div>

      <div className="sidebar-actions">
        <button
          type="button"
          className="btn btn-primary btn-block"
          onClick={onNew}
          data-testid="new-thread-btn"
        >
          + New conversation
        </button>
      </div>

      <div className="sidebar-scroll">
        <div className="sidebar-section-label">Recent</div>

        {showSkeleton ? (
          <div className="thread-skeleton" data-testid="thread-skeleton" aria-hidden>
            <div className="skeleton-row" />
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        ) : error && threads.length === 0 ? (
          <div className="thread-error" role="alert" data-testid="thread-list-error">
            <span>Couldn’t load conversations.</span>
            {onRetry ? (
              <button type="button" className="btn btn-sm" onClick={onRetry} data-testid="thread-list-retry">
                Retry
              </button>
            ) : null}
          </div>
        ) : threads.length === 0 ? (
          <p className="thread-empty" data-testid="thread-empty">
            No conversations yet. Start one to begin.
          </p>
        ) : (
          <ul className="thread-list">
            {threads.map((thread) => {
              const active = thread.thread_id === activeThreadId;
              const when = relativeTime(thread.updated_at);
              const count = thread.message_count;
              return (
                <li key={thread.thread_id}>
                  <button
                    type="button"
                    className={`thread-item${active ? ' active' : ''}`}
                    onClick={() => onSelect(thread.thread_id)}
                    data-testid="thread-item"
                    aria-current={active ? 'true' : undefined}
                  >
                    <span className="thread-item-top">
                      <span className="thread-title">{thread.title || 'New conversation'}</span>
                      {when ? <span className="thread-time">{when}</span> : null}
                    </span>
                    <span className="thread-meta">
                      {count} {count === 1 ? 'message' : 'messages'}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <IntegrationsPanel baseUrl={baseUrl} />
    </aside>
  );
}
