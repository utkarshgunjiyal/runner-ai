import { useState } from 'react';
import type { TimelineItem } from '../../state/runTypes';

/**
 * A focused, collapsible view of tool execution timeline entries — capability id
 * + status + optional safe detail. Collapsed by default. Displays only safe
 * fields (never args/output/prompts/secrets).
 */
export function ToolExecutionCard({
  items,
  defaultOpen = false,
}: {
  items: TimelineItem[];
  defaultOpen?: boolean;
}) {
  const tools = items.filter((i) => i.kind === 'tool');
  const [open, setOpen] = useState(defaultOpen);
  if (tools.length === 0) return null;

  return (
    <div className="tool-exec" data-testid="tool-exec">
      <button
        type="button"
        className="tool-exec-toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        data-testid="tool-exec-toggle"
      >
        <span>{open ? '▾' : '▸'}</span> Tool activity
        <span className="tool-exec-count">{tools.length}</span>
      </button>
      {open ? (
        <ul className="tool-exec-list">
          {tools.map((item) => (
            <li key={item.key} className={`tool-exec-item tool-${item.status ?? 'info'}`}>
              <span className="tool-exec-cap">{item.detail ?? 'capability'}</span>
              <span className="tool-exec-status">{item.label}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
