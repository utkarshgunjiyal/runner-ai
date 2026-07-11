import type { AnswerRound } from '../../state/runTypes';
import { parseAnswer } from '../../lib/format';

/**
 * Renders the assistant answer. A bounded repair produces a second round — prior
 * rounds are shown as superseded so the regeneration is visible, and the active
 * round streams with a cursor until completed. When a completed answer carries a
 * trailing `Sources` list (Phase 44.x comparison output), those sources are
 * surfaced as compact chips (filename + page); opaque evidence ids never render.
 */
export function StreamingMessage({
  rounds,
  streaming,
}: {
  rounds: AnswerRound[];
  streaming: boolean;
}) {
  if (rounds.length === 0) return null;
  const activeIndex = rounds.length - 1;

  return (
    <div className="msg msg-assistant" data-testid="assistant-message">
      {rounds.map((round, index) => {
        const isActive = index === activeIndex;
        const superseded = !isActive;
        // Only lift sources out once the round is finalized — while streaming we
        // show the raw text (including any partial Sources block) verbatim.
        const parsed = round.completed ? parseAnswer(round.text) : { body: round.text, sources: [] };
        return (
          <div
            key={index}
            className={`answer-round${superseded ? ' answer-superseded' : ''}`}
            data-testid={isActive ? 'answer-active' : 'answer-superseded'}
          >
            {superseded ? <div className="answer-round-tag">Superseded draft</div> : null}
            <p className="answer-text">
              {parsed.body}
              {isActive && streaming && !round.completed ? (
                <span className="cursor" data-testid="stream-cursor" aria-hidden>
                  ▍
                </span>
              ) : null}
            </p>
            {parsed.sources.length > 0 ? (
              <div className="source-chips" data-testid="source-chips">
                <span className="source-chips-label">Sources</span>
                {parsed.sources.map((source) => (
                  <span key={source} className="source-chip" data-testid="source-chip">
                    {source}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
