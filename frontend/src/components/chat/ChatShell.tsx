import { useCallback, useEffect, useMemo, useState } from 'react';
import { useAgentRun } from '../../hooks/useAgentRun';
import { useThreads } from '../../hooks/useThreads';
import { uploadDocument } from '../../api/documentsClient';
import type { DocumentUploadResult, ResumeResolution, ThreadDocument } from '../../api/types';
import { canSubmit, isStreamActive } from '../../state/runTypes';
import { RuntimeOutcomeBadge } from '../runtime/RuntimeOutcomeBadge';
import { RuntimeInspector } from '../runtime/RuntimeInspector';
import { ApprovalPanel } from '../hitl/ApprovalPanel';
import { ClarificationPanel } from '../hitl/ClarificationPanel';
import { WaitingContextPanel } from '../hitl/WaitingContextPanel';
import { FailedRunPanel } from '../hitl/FailedRunPanel';
import { DocumentPickerPanel } from '../hitl/DocumentPickerPanel';
import { ThreadSidebar } from '../threads/ThreadSidebar';
import { DocumentSelector } from '../documents/DocumentSelector';
import { MessageList } from './MessageList';
import { Composer } from './Composer';

/** Top-level workspace: threads, documents, streaming answer, HITL, runtime inspector. */
export function ChatShell({ baseUrl = '' }: { baseUrl?: string }) {
  const threads = useThreads(baseUrl);
  const { setActiveThreadId, refreshThreads } = threads;
  const [selectedDocs, setSelectedDocs] = useState<string[]>([]);
  const [threadError, setThreadError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);

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
    setSidebarOpen(false);
    void threads.selectThread(id);
  };

  // "New conversation" creates a thread immediately (POST /threads), which makes
  // it active and clears messages/documents. We abort any active stream + clear
  // run/checkpoint/HITL state first, and surface a safe error on failure.
  const onNewThread = useCallback(async () => {
    reset();
    setSelectedDocs([]);
    setThreadError(null);
    setSidebarOpen(false);
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

  // Header title: backend thread title → first user message → live request → default.
  const activeThread = useMemo(
    () => threads.threads.find((t) => t.thread_id === threads.activeThreadId) ?? null,
    [threads.threads, threads.activeThreadId],
  );
  const headerTitle =
    activeThread?.title?.trim() ||
    threads.messages.find((m) => m.role === 'user')?.content ||
    state.request ||
    'New conversation';

  // Selected documents resolved to {id, filename} for the composer scope chips.
  const selectedDocuments = useMemo(
    () =>
      selectedDocs
        .map((id) => threads.documents.find((d) => d.document_id === id))
        .filter((d): d is ThreadDocument => Boolean(d))
        .map((d) => ({ document_id: d.document_id, filename: d.filename })),
    [selectedDocs, threads.documents],
  );

  const hasRuntimeActivity = state.timeline.length > 0;

  return (
    <div
      className="app-layout"
      data-testid="app-layout"
      data-sidebar-open={sidebarOpen}
      data-inspector-open={inspectorOpen}
    >
      <div
        className="scrim"
        data-testid="scrim"
        onClick={() => {
          setSidebarOpen(false);
          setInspectorOpen(false);
        }}
        aria-hidden
      />

      <ThreadSidebar
        threads={threads.threads}
        activeThreadId={threads.activeThreadId}
        loading={threads.loading}
        error={threads.error}
        baseUrl={baseUrl}
        onSelect={onSelectThread}
        onNew={() => void onNewThread()}
        onRetry={() => void refreshThreads()}
      />

      <div className="chat-shell" data-testid="chat-shell" data-status={state.status}>
        <header className="chat-header">
          <button
            type="button"
            className="icon-btn nav-toggle"
            onClick={() => setSidebarOpen((v) => !v)}
            aria-label="Toggle conversations"
            aria-expanded={sidebarOpen}
            data-testid="sidebar-toggle"
          >
            ☰
          </button>
          <div className="chat-heading">
            <h1 className="chat-title" data-testid="chat-title">
              {headerTitle}
            </h1>
            <span className="chat-subtitle">Autonomous agent workspace</span>
          </div>
          <div className="chat-header-actions">
            <RuntimeOutcomeBadge status={state.status} />
            <button
              type="button"
              className="icon-btn"
              onClick={() => setInspectorOpen((v) => !v)}
              aria-label="Runtime details"
              aria-expanded={inspectorOpen}
              data-testid="inspector-toggle"
            >
              ⚙
              {hasRuntimeActivity ? <span className="icon-dot" aria-hidden /> : null}
            </button>
          </div>
        </header>

        <main className="chat-main">
          {threadError ? (
            <p className="thread-error" role="alert" data-testid="thread-error">
              {threadError}
            </p>
          ) : null}
          <MessageList state={state} history={threads.messages} />

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

          {state.resuming ? (
            <p className="resuming-note" data-testid="resuming-note">
              <span className="cursor" aria-hidden>
                ▍
              </span>
              Continuing the run…
            </p>
          ) : null}
        </main>

        <footer className="chat-footer">
          <div className="chat-footer-inner">
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
              selectedDocuments={selectedDocuments}
              hasDocuments={threads.documents.length > 0}
              onRemoveDoc={toggleDoc}
            />
            {state.status === 'completed' || state.status === 'cancelled' ? (
              <button
                type="button"
                className="btn btn-ghost btn-new"
                onClick={() => void onNewThread()}
                data-testid="new-run-btn"
              >
                New request
              </button>
            ) : null}
          </div>
        </footer>
      </div>

      {inspectorOpen ? (
        <RuntimeInspector
          status={state.status}
          items={state.timeline}
          onClose={() => setInspectorOpen(false)}
        />
      ) : null}
    </div>
  );
}
