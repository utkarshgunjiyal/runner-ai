import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useUpload, type UseUploadParams } from '../hooks/useUpload';
import type { DocumentUploadResult } from '../api/types';

afterEach(() => {
  vi.useRealTimers();
});

const UPLOAD_RESULT: DocumentUploadResult = { document_id: 'd1', job_id: 'j1', status: 'processing' };
const aFile = () => new File(['x'], 'a.pdf');

describe('useUpload polling (Defect 8)', () => {
  it('polls until completed, refreshing on each poll, then stops', async () => {
    vi.useFakeTimers();
    const statuses = ['processing', 'processing', 'completed'];
    let i = 0;
    const pollStatus = vi.fn(async () => ({ status: statuses[i++] ?? 'completed' }));
    const onRefreshDocuments = vi.fn();
    const onUpload = vi.fn(async () => UPLOAD_RESULT);

    const { result } = renderHook(() =>
      useUpload({ onUpload, onRefreshDocuments, pollStatus, pollIntervalMs: 2000, pollMaxMs: 60000 }),
    );

    await act(async () => {
      result.current.selectFile(aFile());
    });

    for (let p = 0; p < 3; p++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
    }
    expect(pollStatus).toHaveBeenCalledTimes(3);
    expect(onRefreshDocuments).toHaveBeenCalledTimes(3);

    // Terminal reached — no further polling.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(pollStatus).toHaveBeenCalledTimes(3);
  });

  it('polls until failed, then stops', async () => {
    vi.useFakeTimers();
    const statuses = ['processing', 'failed'];
    let i = 0;
    const pollStatus = vi.fn(async () => ({ status: statuses[i++] ?? 'failed' }));
    const onRefreshDocuments = vi.fn();
    const onUpload = vi.fn(async () => UPLOAD_RESULT);

    const { result } = renderHook(() =>
      useUpload({ onUpload, onRefreshDocuments, pollStatus, pollIntervalMs: 2000, pollMaxMs: 60000 }),
    );

    await act(async () => {
      result.current.selectFile(aFile());
    });
    for (let p = 0; p < 2; p++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
    }
    expect(pollStatus).toHaveBeenCalledTimes(2);
    expect(onRefreshDocuments).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(pollStatus).toHaveBeenCalledTimes(2);
  });

  it('a poll failure degrades to a stopped state (no throw, no further polls)', async () => {
    vi.useFakeTimers();
    const pollStatus = vi.fn(async () => {
      throw new Error('network down');
    });
    const onRefreshDocuments = vi.fn();
    const onUpload = vi.fn(async () => UPLOAD_RESULT);

    const { result } = renderHook(() =>
      useUpload({ onUpload, onRefreshDocuments, pollStatus, pollIntervalMs: 2000, pollMaxMs: 60000 }),
    );
    await act(async () => {
      result.current.selectFile(aFile());
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(pollStatus).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(pollStatus).toHaveBeenCalledTimes(1);
  });

  it('cancels polling on thread switch (prop change) — no further fetches', async () => {
    vi.useFakeTimers();
    const pollStatus = vi.fn(async () => ({ status: 'processing' }));
    const onUpload = vi.fn(async () => UPLOAD_RESULT);
    const baseProps: UseUploadParams = {
      onUpload,
      pollStatus,
      activeThreadId: 't1',
      pollIntervalMs: 2000,
      pollMaxMs: 60000,
    };

    const { result, rerender } = renderHook((props: UseUploadParams) => useUpload(props), {
      initialProps: baseProps,
    });

    await act(async () => {
      result.current.selectFile(aFile());
    });
    // Switch thread before the first poll fires — must cancel polling.
    rerender({ ...baseProps, activeThreadId: 't2' });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(pollStatus).not.toHaveBeenCalled();
  });

  it('cancels polling on unmount', async () => {
    vi.useFakeTimers();
    const pollStatus = vi.fn(async () => ({ status: 'processing' }));
    const onUpload = vi.fn(async () => UPLOAD_RESULT);

    const { result, unmount } = renderHook(() =>
      useUpload({ onUpload, pollStatus, pollIntervalMs: 2000, pollMaxMs: 60000 }),
    );
    await act(async () => {
      result.current.selectFile(aFile());
    });
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(pollStatus).not.toHaveBeenCalled();
  });
});
