// Small pure display helpers (Phase 45). No React, no network — safe to unit-test.

/** Compact relative time for the thread list (e.g. "just now", "4m", "2h", "3d").
 * Falls back to a short date for anything older than a week. Never throws. */
export function relativeTime(iso: string | undefined, now: number = Date.now()): string {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const diff = Math.max(0, now - then);
  const min = 60_000;
  const hour = 60 * min;
  const day = 24 * hour;
  if (diff < min) return 'just now';
  if (diff < hour) return `${Math.floor(diff / min)}m`;
  if (diff < day) return `${Math.floor(diff / hour)}h`;
  if (diff < 7 * day) return `${Math.floor(diff / day)}d`;
  const d = new Date(then);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export interface ParsedAnswer {
  /** The answer body with the trailing "Sources" block removed (if present). */
  body: string;
  /** Parsed source labels (e.g. "resume.pdf p.1"), de-duplicated, in order. */
  sources: string[];
}

/**
 * Split a deterministic/real answer into its body and its trailing `Sources`
 * list so the UI can render sources as compact chips. Display-only and safe: it
 * never executes anything, only slices the already-sanitized answer text. If no
 * `Sources` section is present, the whole text is the body and sources is empty.
 * Bare evidence ids (E1, E7, …) are never treated as sources.
 */
export function parseAnswer(text: string): ParsedAnswer {
  if (!text) return { body: '', sources: [] };
  const lines = text.split('\n');
  // Find the last line that is exactly the "Sources" heading.
  let headingIndex = -1;
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (lines[i].trim().toLowerCase() === 'sources') {
      headingIndex = i;
      break;
    }
  }
  if (headingIndex === -1) return { body: text.trimEnd(), sources: [] };

  const sources: string[] = [];
  for (let i = headingIndex + 1; i < lines.length; i += 1) {
    const raw = lines[i].trim();
    if (!raw) continue;
    const label = raw.replace(/^[-•*]\s*/, '').trim();
    // Skip placeholders and any bare evidence-id artifacts.
    if (!label || /^\[?E\d+\]?$/i.test(label)) continue;
    if (/^no sources/i.test(label)) continue;
    if (!sources.includes(label)) sources.push(label);
  }
  const body = lines.slice(0, headingIndex).join('\n').trimEnd();
  return { body, sources };
}
