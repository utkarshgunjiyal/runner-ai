// useThreads (Phase 42B): owns the thread list, the active thread, and the
// active thread's persisted messages + documents. Selecting a thread loads its
// history and documents; switching clears the previous thread's data so nothing
// leaks across threads. A monotonic request guard prevents a slow load from a
// previously-selected thread clobbering the current one.

import { useCallback, useRef, useState } from 'react';
import {
  createThread as createThreadRequest,
  listThreads,
  loadMessages,
  loadThreadDocuments,
} from '../api/threadsClient';
import type { ThreadDocument, ThreadMessage, ThreadSummary } from '../api/types';

export interface ThreadsApi {
  threads: ThreadSummary[];
  activeThreadId: string | null;
  messages: ThreadMessage[];
  documents: ThreadDocument[];
  /** True while the initial thread list is loading (drives the sidebar skeleton). */
  loading: boolean;
  /** True when the last list load failed (drives the sidebar error + retry). */
  error: boolean;
  refreshThreads: () => Promise<void>;
  createThread: (title?: string) => Promise<ThreadSummary>;
  selectThread: (id: string | null) => Promise<void>;
  refreshDocuments: (threadId?: string) => Promise<void>;
  setActiveThreadId: (id: string | null) => void;
}

export function useThreads(baseUrl = ''): ThreadsApi {
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ThreadMessage[]>([]);
  const [documents, setDocuments] = useState<ThreadDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  // Guards against out-of-order thread loads overwriting the active thread.
  const selectSeq = useRef(0);

  const refreshThreads = useCallback(async () => {
    // Degrade gracefully when the backend is unreachable (empty sidebar) rather
    // than throwing an unhandled rejection on mount, but surface a load error so
    // the sidebar can offer a retry instead of an ambiguous empty list.
    setLoading(true);
    try {
      const list = await listThreads(baseUrl);
      setThreads(list);
      setError(false);
    } catch {
      setThreads([]);
      setError(true);
    } finally {
      setLoading(false);
    }
  }, [baseUrl]);

  const createThread = useCallback(
    async (title?: string): Promise<ThreadSummary> => {
      const thread = await createThreadRequest(title, baseUrl);
      selectSeq.current += 1; // supersede any in-flight select load
      setThreads((prev) => [thread, ...prev.filter((t) => t.thread_id !== thread.thread_id)]);
      setActiveThreadId(thread.thread_id);
      setMessages([]);
      setDocuments([]);
      return thread;
    },
    [baseUrl],
  );

  const selectThread = useCallback(
    async (id: string | null): Promise<void> => {
      const seq = (selectSeq.current += 1);
      setActiveThreadId(id);
      // Clear immediately so no prior-thread data is ever shown.
      setMessages([]);
      setDocuments([]);
      if (id == null) return;
      try {
        const [msgs, docs] = await Promise.all([
          loadMessages(id, undefined, baseUrl),
          loadThreadDocuments(id, baseUrl),
        ]);
        if (selectSeq.current !== seq) return; // superseded by a newer selection
        setMessages(msgs);
        setDocuments(docs);
      } catch {
        if (selectSeq.current === seq) {
          setMessages([]);
          setDocuments([]);
        }
      }
    },
    [baseUrl],
  );

  const refreshDocuments = useCallback(
    async (threadId?: string): Promise<void> => {
      const id = threadId ?? activeThreadId;
      if (id == null) {
        setDocuments([]);
        return;
      }
      try {
        const docs = await loadThreadDocuments(id, baseUrl);
        setDocuments(docs);
      } catch {
        /* keep the current document list on a transient refresh failure */
      }
    },
    [activeThreadId, baseUrl],
  );

  return {
    threads,
    activeThreadId,
    messages,
    documents,
    loading,
    error,
    refreshThreads,
    createThread,
    selectThread,
    refreshDocuments,
    setActiveThreadId,
  };
}
