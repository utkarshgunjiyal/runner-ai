import { ChatShell } from './components/chat/ChatShell';
import { ErrorBoundary } from './components/ErrorBoundary';

// The backend base URL. Empty by default so requests are same-origin (the Vite
// dev server proxies /agent to the backend), which keeps auth cookies + SSE
// working without CORS friction. Override with VITE_BACKEND_URL for cross-origin.
const BASE_URL = (import.meta.env.VITE_BACKEND_URL as string | undefined) ?? '';

export default function App() {
  return (
    <ErrorBoundary>
      <ChatShell baseUrl={BASE_URL} />
    </ErrorBoundary>
  );
}
