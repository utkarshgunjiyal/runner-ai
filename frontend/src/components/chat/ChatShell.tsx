import { useCallback, useEffect, useState } from 'react';
import { useAgentRun } from '../../hooks/useAgentRun';
import { useThreads } from '../../hooks/useThreads';
import { uploadDocument } from '../../api/documentsClient';
import type { ResumeResolution } from '../../api/types';
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
    void threads.selectThread(id);
  };

  const onNewThread = () => {
    reset();
    setSelectedDocs([]);
    void threads.selectThread(null);
  };

  const onSend = (text: string) => {
    submit(text, threads.activeThreadId, selectedDocs);
  };

  const toggleDoc = (documentId: string) => {
    setSelectedDocs((prev) =>
      prev.includes(documentId) ? prev.filter((id) => id !== documentId) : [...prev, documentId],
    );
  };

  const onUpload = async (file: File) => {
    let threadId = threads.activeThreadId;
    if (!threadId) {
      const created = await threads.createThread();
      threadId = created.thread_id;
    }
    await uploadDocument(file, threadId, baseUrl);
    await threads.refreshDocuments(threadId);
  };

  const isDocumentPause =
    state.pendingAction === 'select_document' && state.documentCandidates.length > 0;

  return (
    <div className="app-layout" data-testid="app-layout">
      <ThreadSidebar
        threads={threads.threads}
        activeThreadId={threads.activeThreadId}
        onSelect={onSelectThread}
        onNew={onNewThread}
      />

      <div className="chat-shell" data-testid="chat-shell" data-status={state.status}>
        <header className="chat-header">
          <div className="brand">
            <span className="brand-mark">◇</span> Runner.ai
          </div>
          <RuntimeOutcomeBadge status={state.status} />
        </header>

        <main className="chat-main">
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
          />
          <Composer
            canSend={canSubmit(state.status)}
            isActive={isStreamActive(state.status)}
            onSend={onSend}
            onCancel={cancel}
          />
          {state.status === 'completed' || state.status === 'cancelled' ? (
            <button type="button" className="btn btn-ghost btn-new" onClick={onNewThread} data-testid="new-run-btn">
              New request
            </button>
          ) : null}
        </footer>
      </div>
    </div>
  );
}
