import { useState } from 'react';
import type { TimelineItem } from '../../state/runTypes';
import { RuntimeEventCard } from './RuntimeEventCard';

/** Collapsible runtime activity panel. Expanded for demos, collapsed by default. */
export function RuntimeTimeline({
  items,
  defaultOpen = false,
}: {
  items: TimelineItem[];
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (items.length === 0) return null;

  return (
    <section className="timeline" data-testid="runtime-timeline">
      <button
        type="button"
        className="timeline-toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span>{open ? '▾' : '▸'}</span> Runtime activity
        <span className="timeline-count">{items.length}</span>
      </button>
      {open ? (
        <ul className="timeline-list">
          {items.map((item: TimelineItem) => (
            <RuntimeEventCard key={item.key} item={item} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}
