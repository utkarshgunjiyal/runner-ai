import { useCallback, useEffect, useState } from 'react';
import { useAgentRun } from '../../hooks/useAgentRun';
import { useThreads } from '../../hooks/useThreads';
import { uploadDocument } from '../../api/documentsClient';
import type { DocumentUploadResult, ResumeResolution } from '../../api/types';
import { canSubmit, isStreamActive } from '../../state/runTypes';
import { RuntimeOutcomeBadge } from '../runtime/RuntimeOutcomeBadge';
import { RuntimeTimeline } from '../runtime/RuntimeTimeline';
import { ToolExecutionCard } from '../runtime/ToolExecutionCard';
import { ApprovalPanel } from '../hitl/ApprovalPanel';
import { ClarificationPanel } from '../hitl/ClarificationPanel';
import { WaitingContextPanel } from '../hitl/WaitingContextPanel';
import { FailedRunPanel } from '../hitl/FailedRunPanel';
import { DocumentPickerPanel } from '../hitl/DocumentPickerPanel';
import { ThreadSidebar } from '../threads/ThreadSidebar';
import { DocumentSelector } from '../documents/DocumentSelector';
import { MessageList } from './MessageList';
import { Composer } from './Composer';

/** Top-level chat experience: threads, documents, streaming answer, HITL panels. */
export function ChatShell({ baseUrl = '' }: { baseUrl?: string }) {
  const threads = useThreads(baseUrl);
  const { setActiveThreadId, refreshThreads } = threads;
  const [selectedDocs, setSelectedDocs] = useState<string[]>([]);
  const [threadError, setThreadError] = useState<string | null>(null);

  const onThreadCreated = useCallback(
    (threadId: string) => {
      // First message auto-created a thread server-side: track it + refresh list.
      setActiveThreadId(threadId);
      void refreshThreads();
    },
    [setActiveThreadId, refreshThreads],
  );

  const { state, submit, cancel, resume, resumeWithDocuments, reset } = useAgentRun(
    baseUrl,
    onThreadCreated,
  );

  // Load the conversation list once on mount.
  useEffect(() => {
    void refreshThreads();
  }, [refreshThreads]);

  const onResolve = (resolution: ResumeResolution) => resume(resolution);

  const onSelectThread = (id: string) => {
    reset(); // abort any active stream + clear run state
    setSelectedDocs([]);
    setThreadError(null);
    void threads.selectThread(id);
  };

  // "New conversation" creates a thread immediately (POST /threads), which makes
  // it active and clears messages/documents. We abort any active stream + clear
  // run/checkpoint/HITL state first, and surface a safe error on failure (no
  // silent reset).
  const onNewThread = useCallback(async () => {
    reset();
    setSelectedDocs([]);
    setThreadError(null);
    try {
      await threads.createThread();
    } catch {
      setThreadError('Could not start a new conversation. Please try again.');
    }
  }, [reset, threads]);

  const onSend = (text: string) => {
    submit(text, threads.activeThreadId, selectedDocs);
  };

  const toggleDoc = (documentId: string) => {
    setSelectedDocs((prev) =>
      prev.includes(documentId) ? prev.filter((id) => id !== documentId) : [...prev, documentId],
    );
  };

  const onUpload = useCallback(
    async (file: File): Promise<DocumentUploadResult> => {
      let threadId = threads.activeThreadId;
      if (!threadId) {
        const created = await threads.createThread();
        threadId = created.thread_id;
      }
      return uploadDocument(file, threadId, baseUrl);
    },
    [threads, baseUrl],
  );

  const onRefreshDocuments = useCallback(() => threads.refreshDocuments(), [threads]);

  const isDocumentPause =
    state.pendingAction === 'select_document' && state.documentCandidates.length > 0;

  return (
    <div className="app-layout" data-testid="app-layout">
      <ThreadSidebar
        threads={threads.threads}
        activeThreadId={threads.activeThreadId}
        onSelect={onSelectThread}
        onNew={() => void onNewThread()}
      />

      <div className="chat-shell" data-testid="chat-shell" data-status={state.status}>
        <header className="chat-header">
          <div className="brand">
            <span className="brand-mark">◇</span> Runner.ai
          </div>
          <RuntimeOutcomeBadge status={state.status} />
        </header>

        <main className="chat-main">
          {threadError ? (
            <p className="thread-error" role="alert" data-testid="thread-error">
              {threadError}
            </p>
          ) : null}
          <MessageList state={state} history={threads.messages} />

          <ToolExecutionCard items={state.timeline} />
          <RuntimeTimeline items={state.timeline} />

          {isDocumentPause ? (
            <DocumentPickerPanel
              candidates={state.documentCandidates}
              resuming={state.resuming}
              onConfirm={resumeWithDocuments}
            />
          ) : null}
          {state.status === 'waiting_for_user' && !isDocumentPause ? (
            <ClarificationPanel pendingReason={state.pendingReason} resuming={state.resuming} onResolve={onResolve} />
          ) : null}
          {state.status === 'waiting_for_approval' ? (
            <ApprovalPanel pendingReason={state.pendingReason} resuming={state.resuming} onResolve={onResolve} />
          ) : null}
          {state.status === 'waiting_for_context' || state.status === 'waiting_for_replan' ? (
            <WaitingContextPanel status={state.status} pendingReason={state.pendingReason} />
          ) : null}
          {state.status === 'failed' ? (
            <FailedRunPanel
              error={state.error}
              canRetry={state.error?.retryable === true && state.request != null}
              onRetry={() => {
                if (state.request) submit(state.request, threads.activeThreadId, selectedDocs);
              }}
            />
          ) : null}

          {state.resuming ? <p className="resuming-note" data-testid="resuming-note">Continuing the run…</p> : null}
        </main>

        <footer className="chat-footer">
          <DocumentSelector
            documents={threads.documents}
            selectedIds={selectedDocs}
            onToggle={toggleDoc}
            onUpload={onUpload}
            activeThreadId={threads.activeThreadId}
            onRefreshDocuments={onRefreshDocuments}
            baseUrl={baseUrl}
          />
          <Composer
            canSend={canSubmit(state.status)}
            isActive={isStreamActive(state.status)}
            onSend={onSend}
            onCancel={cancel}
          />
          {state.status === 'completed' || state.status === 'cancelled' ? (
            <button type="button" className="btn btn-ghost btn-new" onClick={() => void onNewThread()} data-testid="new-run-btn">
              New request
            </button>
          ) : null}
        </footer>
      </div>
    </div>
  );
}
