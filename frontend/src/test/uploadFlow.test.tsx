import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DocumentSelector } from '../components/documents/DocumentSelector';
import type { ThreadDocument } from '../api/types';

afterEach(() => vi.unstubAllGlobals());

const NO_DOCS: ThreadDocument[] = [];
const aFile = () => new File(['pdf-bytes'], 'report.pdf', { type: 'application/pdf' });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderSelector(onUpload: (file: File) => Promise<void> | void) {
  return render(
    <DocumentSelector
      documents={NO_DOCS}
      selectedIds={[]}
      onToggle={vi.fn()}
      onUpload={onUpload}
      // A poller that never resolves keeps background polling inert in these UI tests.
      pollStatus={() => new Promise(() => {})}
    />,
  );
}

describe('DocumentSelector upload flow (Defect 8)', () => {
  it('keeps the filename visible and shows Uploading… during an active upload', async () => {
    const d = deferred<void>();
    renderSelector(() => d.promise);

    fireEvent.change(screen.getByTestId('doc-upload-input'), { target: { files: [aFile()] } });

    expect(await screen.findByTestId('doc-upload-filename')).toHaveTextContent('report.pdf');
    expect(screen.getByText('Uploading…')).toBeInTheDocument();
    expect(screen.getByTestId('doc-upload-input')).toBeDisabled();

    d.resolve();
    await waitFor(() => expect(screen.queryByTestId('doc-upload-filename')).not.toBeInTheDocument());
  });

  it('clears the selected file only after a successful upload', async () => {
    const onUpload = vi.fn(async () => {});
    renderSelector(onUpload);

    fireEvent.change(screen.getByTestId('doc-upload-input'), { target: { files: [aFile()] } });

    await waitFor(() => expect(onUpload).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByTestId('doc-upload-filename')).not.toBeInTheDocument());
    expect(screen.getByTestId('doc-upload-input')).not.toBeDisabled();
  });

  it('keeps the file selected and shows a SAFE retry message on failure', async () => {
    const onUpload = vi
      .fn<(file: File) => Promise<void>>()
      .mockRejectedValueOnce(new Error('backend blew up: secret stack trace'));
    renderSelector(onUpload);

    fireEvent.change(screen.getByTestId('doc-upload-input'), { target: { files: [aFile()] } });

    const err = await screen.findByTestId('doc-upload-error');
    expect(err).toHaveTextContent('Upload failed. Please try again.');
    // never surface raw backend text
    expect(screen.queryByText(/secret stack trace/)).not.toBeInTheDocument();
    // the file stays selected + a retry path is offered
    expect(screen.getByTestId('doc-upload-filename')).toHaveTextContent('report.pdf');

    onUpload.mockResolvedValueOnce(undefined);
    fireEvent.click(screen.getByTestId('doc-upload-retry'));
    await waitFor(() => expect(onUpload).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByTestId('doc-upload-error')).not.toBeInTheDocument());
  });

  it('disables the upload control during an upload (prevents duplicate submits)', async () => {
    const d = deferred<void>();
    const onUpload = vi.fn(() => d.promise);
    renderSelector(onUpload);

    const input = screen.getByTestId('doc-upload-input');
    fireEvent.change(input, { target: { files: [aFile()] } });
    await screen.findByTestId('doc-upload-filename');
    expect(input).toBeDisabled();

    // A second attempt while uploading must not trigger another upload.
    fireEvent.change(input, { target: { files: [aFile()] } });
    expect(onUpload).toHaveBeenCalledTimes(1);

    d.resolve();
    await waitFor(() => expect(input).not.toBeDisabled());
  });
});
