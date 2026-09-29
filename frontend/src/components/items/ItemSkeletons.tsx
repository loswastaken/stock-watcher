import { Skeleton } from '../ui/skeleton';

export function ItemCardSkeleton() {
  return (
    <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800/80 dark:bg-zinc-900/50">
      <Skeleton className="aspect-[4/3] w-full rounded-none" />
      <div className="space-y-2.5 p-4">
        <Skeleton className="h-4 w-4/5" />
        <Skeleton className="h-3 w-1/3" />
        <div className="flex justify-between pt-2">
          <Skeleton className="h-3 w-1/2" />
          <Skeleton className="h-4 w-12" />
        </div>
      </div>
    </div>
  );
}

export function ItemRowSkeleton() {
  return (
    <div className="flex items-center gap-4 px-4 py-3">
      <Skeleton className="size-14 rounded-lg" />
      <div className="flex-1 space-y-2">
        <Skeleton className="h-4 w-1/3" />
        <Skeleton className="h-3 w-1/2" />
      </div>
      <Skeleton className="hidden h-5 w-20 rounded-full md:block" />
    </div>
  );
}
