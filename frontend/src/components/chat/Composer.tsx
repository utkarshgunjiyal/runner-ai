import { useState, type FormEvent, type KeyboardEvent } from 'react';

/** Request composer with send/cancel. Send is disabled while a run is active. */
export function Composer({
  canSend,
  isActive,
  onSend,
  onCancel,
}: {
  canSend: boolean;
  isActive: boolean;
  onSend: (text: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState('');

  const send = () => {
    const trimmed = text.trim();
    if (!trimmed || !canSend) return;
    onSend(trimmed);
    setText('');
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    send();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <form className="composer" onSubmit={onSubmit}>
      <textarea
        className="composer-input"
        placeholder="Ask Runner.ai to do something…"
        value={text}
        rows={2}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        aria-label="Request"
      />
      {isActive ? (
        <button type="button" className="btn btn-ghost" onClick={onCancel} data-testid="cancel-btn">
          Cancel
        </button>
      ) : (
        <button type="submit" className="btn btn-primary" disabled={!canSend || !text.trim()} data-testid="send-btn">
          Send
        </button>
      )}
    </form>
  );
}
