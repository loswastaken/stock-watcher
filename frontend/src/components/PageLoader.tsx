import { Loader2 } from 'lucide-react';

export function PageLoader() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center">
      <Loader2 className="size-5 animate-spin text-zinc-400" />
    </div>
  );
}
