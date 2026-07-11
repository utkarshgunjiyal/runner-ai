import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StreamingMessage } from '../components/chat/StreamingMessage';
import type { AnswerRound } from '../state/runTypes';

const COMPARISON = [
  'Comparison of the selected documents for: Compare skills',
  '',
  'Document 1 — resume.pdf',
  '',
  'Languages',
  '- Python',
  '',
  'Similarities',
  '- Both documents list Python.',
  '',
  'Sources',
  '- resume.pdf p.1',
  '- other.pdf p.2',
].join('\n');

describe('StreamingMessage source chips', () => {
  it('renders a completed comparison with source chips and the sections preserved', () => {
    const rounds: AnswerRound[] = [{ text: COMPARISON, completed: true }];
    render(<StreamingMessage rounds={rounds} streaming={false} />);

    // Section structure preserved in the body.
    const body = screen.getByTestId('answer-active');
    expect(body).toHaveTextContent('Document 1 — resume.pdf');
    expect(body).toHaveTextContent('Similarities');

    // Sources surfaced as chips.
    const chips = screen.getAllByTestId('source-chip').map((c) => c.textContent);
    expect(chips).toEqual(['resume.pdf p.1', 'other.pdf p.2']);
  });

  it('never renders opaque E# evidence ids as sources', () => {
    const text = ['Answer.', '', 'Sources', '- E7', '- resume.pdf p.1'].join('\n');
    render(<StreamingMessage rounds={[{ text, completed: true }]} streaming={false} />);
    const chips = screen.getAllByTestId('source-chip').map((c) => c.textContent);
    expect(chips).toEqual(['resume.pdf p.1']);
    expect(screen.queryByText('E7')).not.toBeInTheDocument();
  });

  it('shows a streaming cursor and no chips while still streaming', () => {
    render(<StreamingMessage rounds={[{ text: 'partial…', completed: false }]} streaming />);
    expect(screen.getByTestId('stream-cursor')).toBeInTheDocument();
    expect(screen.queryByTestId('source-chip')).not.toBeInTheDocument();
  });
});
