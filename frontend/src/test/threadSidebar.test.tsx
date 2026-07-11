import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ThreadSidebar } from '../components/threads/ThreadSidebar';
import type { ThreadSummary } from '../api/types';

const THREADS: ThreadSummary[] = [
  { thread_id: 't1', title: 'Compare resumes', updated_at: '2026-07-11T11:59:00Z', message_count: 3 },
  { thread_id: 't2', title: '', updated_at: '2026-07-10T09:00:00Z', message_count: 1 },
];

function noop() {}

describe('ThreadSidebar states', () => {
  it('shows a loading skeleton while the initial list loads', () => {
    render(<ThreadSidebar threads={[]} activeThreadId={null} loading onSelect={noop} onNew={noop} />);
    expect(screen.getByTestId('thread-skeleton')).toBeInTheDocument();
  });

  it('shows an empty state when there are no conversations', () => {
    render(<ThreadSidebar threads={[]} activeThreadId={null} onSelect={noop} onNew={noop} />);
    expect(screen.getByTestId('thread-empty')).toBeInTheDocument();
  });

  it('shows an error with a retry action', () => {
    const onRetry = vi.fn();
    render(<ThreadSidebar threads={[]} activeThreadId={null} error onSelect={noop} onNew={noop} onRetry={onRetry} />);
    expect(screen.getByTestId('thread-list-error')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('thread-list-retry'));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('renders titles (with fallback), message counts, and marks the active thread', () => {
    render(<ThreadSidebar threads={THREADS} activeThreadId="t1" onSelect={noop} onNew={noop} />);
    expect(screen.getByText('Compare resumes')).toBeInTheDocument();
    // Untitled thread falls back to a friendly label, never a raw id.
    expect(screen.getByText('New conversation')).toBeInTheDocument();
    expect(screen.queryByText('t2')).not.toBeInTheDocument();
    expect(screen.getByText('3 messages')).toBeInTheDocument();
    expect(screen.getByText('1 message')).toBeInTheDocument();
    const active = screen.getAllByTestId('thread-item').find((el) => el.getAttribute('aria-current') === 'true');
    expect(active).toHaveTextContent('Compare resumes');
  });

  it('selects a thread on click', () => {
    const onSelect = vi.fn();
    render(<ThreadSidebar threads={THREADS} activeThreadId={null} onSelect={onSelect} onNew={noop} />);
    fireEvent.click(screen.getByText('Compare resumes'));
    expect(onSelect).toHaveBeenCalledWith('t1');
  });
});
