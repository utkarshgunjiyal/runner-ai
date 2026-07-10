import type { TimelineItem } from '../../state/runTypes';

const ICON: Record<string, string> = {
  context: '▚',
  retrieval: '⌕',
  planner: '❖',
  tool: '⚙',
  evaluation: '✓',
  repair: '↻',
};

/** One safe timeline entry. Tool events render their capability id + status. */
export function RuntimeEventCard({ item }: { item: TimelineItem }) {
  return (
    <li className={`timeline-item timeline-${item.status ?? 'info'}`} data-kind={item.kind} data-testid="timeline-item">
      <span className="timeline-icon" aria-hidden>{ICON[item.kind] ?? '•'}</span>
      <span className="timeline-label">{item.label}</span>
      {item.detail ? <span className="timeline-detail">{item.detail}</span> : null}
    </li>
  );
}
