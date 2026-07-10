import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ClarificationPanel } from '../components/hitl/ClarificationPanel';
import { ApprovalPanel } from '../components/hitl/ApprovalPanel';
import { WaitingContextPanel } from '../components/hitl/WaitingContextPanel';
import { FailedRunPanel } from '../components/hitl/FailedRunPanel';

describe('ClarificationPanel', () => {
  it('sends a clarification resolution with the typed value', () => {
    const onResolve = vi.fn();
    render(<ClarificationPanel pendingReason="need info" resuming={false} onResolve={onResolve} />);
    fireEvent.change(screen.getByLabelText('Clarification'), { target: { value: 'the Q3 report' } });
    fireEvent.click(screen.getByTestId('clarify-continue'));
    expect(onResolve).toHaveBeenCalledWith({ kind: 'clarification', value: 'the Q3 report' });
  });

  it('disables continue while resuming (no duplicate submit)', () => {
    const onResolve = vi.fn();
    render(<ClarificationPanel pendingReason={null} resuming onResolve={onResolve} />);
    expect(screen.getByTestId('clarify-continue')).toBeDisabled();
  });
});

describe('ApprovalPanel', () => {
  it('sends an approval resolution', () => {
    const onResolve = vi.fn();
    render(<ApprovalPanel pendingReason="approve this?" resuming={false} onResolve={onResolve} />);
    fireEvent.click(screen.getByTestId('approve-btn'));
    expect(onResolve).toHaveBeenCalledWith({ kind: 'approval', value: true, reason: undefined });
  });

  it('sends a rejection resolution with a reason', () => {
    const onResolve = vi.fn();
    render(<ApprovalPanel pendingReason="approve this?" resuming={false} onResolve={onResolve} />);
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'not safe' } });
    fireEvent.click(screen.getByTestId('reject-btn'));
    expect(onResolve).toHaveBeenCalledWith({ kind: 'rejection', value: false, reason: 'not safe' });
  });

  it('disables both actions while resuming', () => {
    render(<ApprovalPanel pendingReason={null} resuming onResolve={vi.fn()} />);
    expect(screen.getByTestId('approve-btn')).toBeDisabled();
    expect(screen.getByTestId('reject-btn')).toBeDisabled();
  });
});

describe('WaitingContextPanel', () => {
  it('shows a safe deferred state and no continuation action', () => {
    render(<WaitingContextPanel status="waiting_for_context" pendingReason="more context needed" />);
    expect(screen.getByTestId('deferred-panel')).toBeInTheDocument();
    expect(screen.getByText('more context needed')).toBeInTheDocument();
    // no resume/continue button in a deferred state
    expect(screen.queryByRole('button')).toBeNull();
  });
});

describe('FailedRunPanel', () => {
  it('shows a retry when the failure is retryable', () => {
    const onRetry = vi.fn();
    render(<FailedRunPanel error={{ message: 'interrupted', retryable: true }} canRetry onRetry={onRetry} />);
    fireEvent.click(screen.getByTestId('retry-btn'));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('shows a session-expired state without retry', () => {
    render(<FailedRunPanel error={{ message: 'expired', sessionExpired: true }} canRetry={false} onRetry={vi.fn()} />);
    expect(screen.getByText('Session expired')).toBeInTheDocument();
    expect(screen.queryByTestId('retry-btn')).toBeNull();
  });
});
