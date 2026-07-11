import type { RunStatus, TimelineItem } from '../../state/runTypes';
import { RuntimeOutcomeBadge } from './RuntimeOutcomeBadge';
import { ToolExecutionCard } from './ToolExecutionCard';
import { RuntimeTimeline } from './RuntimeTimeline';

/**
 * The right-hand runtime inspector (Phase 45). Keeps execution transparency out
 * of the conversation: a status summary, tool activity, and the safe runtime
 * timeline. Collapsed by default at the app level (this panel only renders when
 * the user opens it from the header); on smaller screens it becomes a sheet.
 *
 * Shows SAFE metadata only — never chain-of-thought, prompts, secrets, auth
 * headers, provider tokens, or full private tool payloads.
 */
export function RuntimeInspector({
  status,
  items,
  onClose,
}: {
  status: RunStatus;
  items: TimelineItem[];
  onClose: () => void;
}) {
  const hasActivity = items.length > 0;
  return (
    <aside className="runtime-inspector" data-testid="runtime-inspector" aria-label="Runtime details">
      <div className="inspector-header">
        <h2 className="inspector-title">Runtime details</h2>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close runtime details" data-testid="inspector-close">
          ✕
        </button>
      </div>
      <div className="inspector-body">
        <div className="inspector-summary">
          Status <RuntimeOutcomeBadge status={status} />
        </div>
        {hasActivity ? (
          <>
            <ToolExecutionCard items={items} />
            <RuntimeTimeline items={items} />
          </>
        ) : (
          <p className="inspector-empty" data-testid="inspector-empty">
            No runtime activity yet. Steps will appear here as a request runs.
          </p>
        )}
      </div>
    </aside>
  );
}
