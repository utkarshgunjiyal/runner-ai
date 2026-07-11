import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { Composer } from '../components/chat/Composer';

function noop() {}

describe('Composer keyboard + scope', () => {
  it('sends on Enter and clears the input; Shift+Enter does not send', () => {
    const onSend = vi.fn();
    render(<Composer canSend isActive={false} onSend={onSend} onCancel={noop} />);
    const input = screen.getByLabelText('Message Runner.ai') as HTMLTextAreaElement;

    fireEvent.change(input, { target: { value: 'hello world' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith('hello world');
    expect(input.value).toBe('');
  });

  it('does not submit blank input, preventing empty duplicate sends', () => {
    const onSend = vi.fn();
    render(<Composer canSend isActive={false} onSend={onSend} onCancel={noop} />);
    const input = screen.getByLabelText('Message Runner.ai');
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
  });

  it('shows a Stop button (not Send) while a run is active', () => {
    const onCancel = vi.fn();
    render(<Composer canSend={false} isActive onSend={noop} onCancel={onCancel} />);
    expect(screen.queryByTestId('send-btn')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('cancel-btn'));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it('renders the all-documents scope when documents exist and none are selected', () => {
    render(<Composer canSend isActive={false} onSend={noop} onCancel={noop} hasDocuments />);
    expect(screen.getByTestId('scope-all')).toBeInTheDocument();
    expect(screen.queryByTestId('scope-chip')).not.toBeInTheDocument();
  });

  it('renders the no-documents scope when the thread has no documents', () => {
    render(<Composer canSend isActive={false} onSend={noop} onCancel={noop} hasDocuments={false} />);
    expect(screen.getByTestId('scope-none')).toBeInTheDocument();
  });

  it('renders selected-document chips and removes one on click', () => {
    const onRemoveDoc = vi.fn();
    render(
      <Composer
        canSend
        isActive={false}
        onSend={noop}
        onCancel={noop}
        hasDocuments
        selectedDocuments={[
          { document_id: 'd1', filename: 'resume.pdf' },
          { document_id: 'd2', filename: 'cover.pdf' },
        ]}
        onRemoveDoc={onRemoveDoc}
      />,
    );
    expect(screen.getAllByTestId('scope-chip')).toHaveLength(2);
    fireEvent.click(screen.getByTestId('scope-remove-d1'));
    expect(onRemoveDoc).toHaveBeenCalledWith('d1');
  });
});
