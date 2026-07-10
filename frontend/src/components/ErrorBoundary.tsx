import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}
interface State {
  hasError: boolean;
}

/** Top-level safety net — a render error never leaks internals or blanks the app. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // Intentionally not surfaced to the UI — no stack traces / internals shown.
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="app-error" role="alert">
          <h2>Something went wrong</h2>
          <p>The interface hit an unexpected error. Please reload the page.</p>
        </div>
      );
    }
    return this.props.children;
  }
}
