import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { DocumentPickerPanel } from '../components/hitl/DocumentPickerPanel';
import type { DocumentCandidate } from '../api/types';

const CANDIDATES: DocumentCandidate[] = [
  { document_id: 'd1', filename: 'q3-report.pdf', created_at: '1' },
  { document_id: 'd2', filename: 'q4-report.pdf', created_at: '2' },
];

describe('DocumentPickerPanel', () => {
  it('renders each candidate as a selectable option', () => {
    render(<DocumentPickerPanel candidates={CANDIDATES} resuming={false} onConfirm={vi.fn()} />);
    expect(screen.getByText('q3-report.pdf')).toBeInTheDocument();
    expect(screen.getByText('q4-report.pdf')).toBeInTheDocument();
  });

  it('confirming resumes with the selected document ids', () => {
    const onConfirm = vi.fn();
    render(<DocumentPickerPanel candidates={CANDIDATES} resuming={false} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByLabelText('q4-report.pdf'));
    fireEvent.click(screen.getByTestId('doc-picker-confirm'));
    expect(onConfirm).toHaveBeenCalledWith(['d2']);
  });

  it('disables confirm until at least one document is selected', () => {
    render(<DocumentPickerPanel candidates={CANDIDATES} resuming={false} onConfirm={vi.fn()} />);
    expect(screen.getByTestId('doc-picker-confirm')).toBeDisabled();
  });

  it('disables confirm while resuming (no duplicate resume)', () => {
    render(<DocumentPickerPanel candidates={CANDIDATES} resuming onConfirm={vi.fn()} />);
    expect(screen.getByTestId('doc-picker-confirm')).toBeDisabled();
  });
});
