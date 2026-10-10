import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Logo } from './Logo';
import { Button } from './ui/button';

/** Last resort for a render error anywhere in the app: a reload screen instead of a blank page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Unhandled error', error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="flex min-h-dvh flex-col items-center justify-center gap-6 bg-zinc-50 p-6 dark:bg-zinc-950">
        <Logo className="size-10" />
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="flex items-center gap-2 text-sm text-zinc-600 dark:text-zinc-400">
            <AlertTriangle className="size-4" /> Something went wrong.
          </div>
          {error.message && <p className="max-w-sm break-words text-xs text-zinc-500">{error.message}</p>}
          <Button size="sm" onClick={() => window.location.reload()}>
            <RefreshCw /> Reload
          </Button>
        </div>
      </div>
    );
  }
}
