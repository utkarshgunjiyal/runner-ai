import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ToolExecutionCard } from '../components/runtime/ToolExecutionCard';
import { RuntimeTimeline } from '../components/runtime/RuntimeTimeline';
import type { TimelineItem } from '../state/runTypes';

const ITEMS: TimelineItem[] = [
  { key: 'k1', kind: 'tool', label: 'completed', detail: 'search.v1', status: 'ok' },
];

describe('runtime activity collapsed by default (Defect 10)', () => {
  it('ToolExecutionCard is collapsed by default and expands on click', () => {
    render(<ToolExecutionCard items={ITEMS} />);
    const toggle = screen.getByTestId('tool-exec-toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('search.v1')).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('search.v1')).toBeInTheDocument();
  });

  it('RuntimeTimeline is collapsed by default', () => {
    render(<RuntimeTimeline items={ITEMS} />);
    const toggle = screen.getByRole('button', { name: /Runtime activity/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('search.v1')).not.toBeInTheDocument();
  });
});
