import { Loader2, WifiOff } from 'lucide-react';
import { Button } from './ui/button';
import { Logo } from './Logo';

export function FullScreenLoader({ error, onRetry }: { error?: boolean; onRetry?: () => void }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-6 bg-zinc-50 p-6 dark:bg-zinc-950">
      <Logo className="size-10" />
      {error ? (
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="flex items-center gap-2 text-sm text-zinc-600 dark:text-zinc-400">
            <WifiOff className="size-4" /> Can't reach the server.
          </div>
          {onRetry && (
            <Button size="sm" onClick={onRetry}>
              Try again
            </Button>
          )}
        </div>
      ) : (
        <Loader2 className="size-5 animate-spin text-zinc-400" />
      )}
    </div>
  );
}
