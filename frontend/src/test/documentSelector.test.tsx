import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DocumentSelector } from '../components/documents/DocumentSelector';
import { uploadDocument } from '../api/documentsClient';
import type { ThreadDocument } from '../api/types';

afterEach(() => vi.unstubAllGlobals());

const DOCS: ThreadDocument[] = [
  { document_id: 'd1', filename: 'ready.pdf', status: 'completed', page_count: 2, created_at: '1' },
  { document_id: 'd2', filename: 'indexing.pdf', status: 'processing', page_count: 0, created_at: '2' },
];

describe('DocumentSelector', () => {
  it('toggling a completed document reports its id', () => {
    const onToggle = vi.fn();
    render(
      <DocumentSelector documents={DOCS} selectedIds={[]} onToggle={onToggle} onUpload={vi.fn()} />,
    );
    fireEvent.click(screen.getByTestId('doc-checkbox-d1'));
    expect(onToggle).toHaveBeenCalledWith('d1');
  });

  it('reflects the current selection and disables non-completed documents', () => {
    render(
      <DocumentSelector documents={DOCS} selectedIds={['d1']} onToggle={vi.fn()} onUpload={vi.fn()} />,
    );
    expect(screen.getByTestId('doc-checkbox-d1')).toBeChecked();
    expect(screen.getByTestId('doc-checkbox-d2')).toBeDisabled();
    expect(screen.getByText('processing')).toBeInTheDocument();
  });

  it('uploading a file calls the documents client with multipart form data', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init: init ?? {} });
      return new Response(JSON.stringify({ document_id: 'dNew', job_id: 'j1', status: 'processing' }), {
        status: 202,
        headers: { 'content-type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const onUpload = async (file: File) => {
      await uploadDocument(file, 't1');
    };
    render(
      <DocumentSelector documents={DOCS} selectedIds={[]} onToggle={vi.fn()} onUpload={onUpload} />,
    );

    const file = new File(['pdf-bytes'], 'new.pdf', { type: 'application/pdf' });
    fireEvent.change(screen.getByTestId('doc-upload-input'), { target: { files: [file] } });

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(calls[0].url).toContain('/documents/upload');
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].init.credentials).toBe('include');
    expect(calls[0].init.body).toBeInstanceOf(FormData);
    const form = calls[0].init.body as FormData;
    expect(form.get('thread_id')).toBe('t1');
    expect((form.get('file') as File).name).toBe('new.pdf');
  });
});
