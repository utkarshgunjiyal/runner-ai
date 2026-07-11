import { useRef, useState, type FormEvent, type KeyboardEvent } from 'react';

export interface ScopeDoc {
  document_id: string;
  filename: string;
}

/**
 * Request composer (Phase 45): a sticky, auto-growing multiline input with a
 * document-scope summary above it. Enter sends, Shift+Enter inserts a newline.
 * While a run streams, the send button becomes a Stop button. A synchronous
 * in-flight guard plus the `canSend` gate prevent duplicate submissions.
 */
export function Composer({
  canSend,
  isActive,
  onSend,
  onCancel,
  selectedDocuments = [],
  hasDocuments = false,
  onRemoveDoc,
}: {
  canSend: boolean;
  isActive: boolean;
  onSend: (text: string) => void;
  onCancel: () => void;
  selectedDocuments?: ScopeDoc[];
  hasDocuments?: boolean;
  onRemoveDoc?: (documentId: string) => void;
}) {
  const [text, setText] = useState('');
  const sendingRef = useRef(false);

  const send = () => {
    const trimmed = text.trim();
    if (!trimmed || !canSend || sendingRef.current) return;
    sendingRef.current = true;
    onSend(trimmed);
    setText('');
    // Release on the next tick; `canSend` flips to false once a run starts.
    queueMicrotask(() => {
      sendingRef.current = false;
    });
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
    <div className="composer-wrap">
      <div className="composer-scope" data-testid="composer-scope">
        {selectedDocuments.length > 0 ? (
          <>
            <span className="scope-lead">Scope:</span>
            {selectedDocuments.map((doc) => (
              <span key={doc.document_id} className="scope-chip" data-testid="scope-chip">
                <span className="doc-name">{doc.filename}</span>
                {onRemoveDoc ? (
                  <button
                    type="button"
                    className="scope-chip-remove"
                    onClick={() => onRemoveDoc(doc.document_id)}
                    aria-label={`Remove ${doc.filename} from scope`}
                    data-testid={`scope-remove-${doc.document_id}`}
                  >
                    ×
                  </button>
                ) : null}
              </span>
            ))}
          </>
        ) : hasDocuments ? (
          <span className="scope-all" data-testid="scope-all">
            ◆ Searching all documents in this conversation
          </span>
        ) : (
          <span className="scope-all" data-testid="scope-none">
            No documents attached — answering from the conversation
          </span>
        )}
      </div>

      <form className="composer" onSubmit={onSubmit}>
        <textarea
          className="composer-input"
          placeholder="Ask Runner.ai to search, summarize, compare, or run a task…"
          value={text}
          rows={1}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          aria-label="Message Runner.ai"
        />
        {isActive ? (
          <button type="button" className="btn btn-danger" onClick={onCancel} data-testid="cancel-btn">
            Stop
          </button>
        ) : (
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!canSend || !text.trim()}
            data-testid="send-btn"
          >
            Send
          </button>
        )}
      </form>
    </div>
  );
}
