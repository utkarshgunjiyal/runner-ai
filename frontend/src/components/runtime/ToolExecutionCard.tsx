import type { TimelineItem } from '../../state/runTypes';

/**
 * A focused view of tool execution timeline entries — capability id + status +
 * optional safe detail. Displays only safe fields (never args/output/secrets).
 */
export function ToolExecutionCard({ items }: { items: TimelineItem[] }) {
  const tools = items.filter((i) => i.kind === 'tool');
  if (tools.length === 0) return null;
  return (
    <div className="tool-exec" data-testid="tool-exec">
      <h4 className="tool-exec-title">Tool activity</h4>
      <ul className="tool-exec-list">
        {tools.map((item) => (
          <li key={item.key} className={`tool-exec-item tool-${item.status ?? 'info'}`}>
            <span className="tool-exec-cap">{item.detail ?? 'capability'}</span>
            <span className="tool-exec-status">{item.label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
