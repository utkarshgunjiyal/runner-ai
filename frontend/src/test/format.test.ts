import { describe, expect, it } from 'vitest';
import { parseAnswer, relativeTime } from '../lib/format';

describe('relativeTime', () => {
  const now = Date.parse('2026-07-11T12:00:00Z');
  it('formats recent times compactly', () => {
    expect(relativeTime('2026-07-11T11:59:30Z', now)).toBe('just now');
    expect(relativeTime('2026-07-11T11:56:00Z', now)).toBe('4m');
    expect(relativeTime('2026-07-11T10:00:00Z', now)).toBe('2h');
    expect(relativeTime('2026-07-08T12:00:00Z', now)).toBe('3d');
  });
  it('is safe on missing/invalid input', () => {
    expect(relativeTime(undefined, now)).toBe('');
    expect(relativeTime('not-a-date', now)).toBe('');
  });
});

describe('parseAnswer', () => {
  it('splits the trailing Sources block into de-duplicated chips', () => {
    const text = [
      'Document 1 — resume.pdf',
      'Languages',
      '- Python',
      '',
      'Sources',
      '- resume.pdf p.1',
      '- resume.pdf p.1',
      '- other.pdf p.2',
    ].join('\n');
    const { body, sources } = parseAnswer(text);
    expect(sources).toEqual(['resume.pdf p.1', 'other.pdf p.2']);
    expect(body).not.toContain('Sources');
    expect(body).toContain('- Python');
  });

  it('never treats bare evidence ids as sources', () => {
    const text = ['Answer body.', '', 'Sources', '- E7', '- [E1]', '- resume.pdf p.3'].join('\n');
    const { sources } = parseAnswer(text);
    expect(sources).toEqual(['resume.pdf p.3']);
  });

  it('returns the whole text as body when there is no Sources section', () => {
    const { body, sources } = parseAnswer('Just a plain answer.');
    expect(body).toBe('Just a plain answer.');
    expect(sources).toEqual([]);
  });
});
