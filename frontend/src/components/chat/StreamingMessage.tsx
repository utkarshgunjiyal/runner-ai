import type { AnswerRound } from '../../state/runTypes';

/**
 * Renders the assistant answer. A bounded repair produces a second round — prior
 * rounds are shown as superseded so the regeneration is visible, and the active
 * round streams with a cursor until completed.
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
        return (
          <div
            key={index}
            className={`answer-round${superseded ? ' answer-superseded' : ''}`}
            data-testid={isActive ? 'answer-active' : 'answer-superseded'}
          >
            {superseded ? <div className="answer-round-tag">Superseded draft</div> : null}
            <p className="answer-text">
              {round.text}
              {isActive && streaming && !round.completed ? (
                <span className="cursor" data-testid="stream-cursor" aria-hidden>▍</span>
              ) : null}
            </p>
          </div>
        );
      })}
    </div>
  );
}
