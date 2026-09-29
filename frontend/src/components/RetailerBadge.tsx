import { monogram } from '@/lib/retailers';
import type { Retailer } from '@/lib/types';
import { cn } from '@/lib/utils';

/** Brand-colored circle with the store's initials (logos aren't bundled). */
export function RetailerMonogram({ retailer, className }: { retailer: Pick<Retailer, 'name' | 'color'>; className?: string }) {
  return (
    <span
      aria-hidden
      style={{ backgroundColor: retailer.color }}
      className={cn(
        'inline-flex size-10 shrink-0 select-none items-center justify-center rounded-full text-sm font-semibold text-white ring-1 ring-black/5 dark:ring-white/10',
        className,
      )}
    >
      {monogram(retailer.name)}
    </span>
  );
}

/** Small pill: monogram dot + store name. */
export function RetailerChip({ retailer, className }: { retailer: Retailer; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex max-w-full items-center gap-1.5 rounded-full bg-white/90 py-0.5 pl-0.5 pr-2 text-[11px] font-medium text-zinc-800 shadow-sm ring-1 ring-zinc-900/5 backdrop-blur dark:bg-zinc-800/90 dark:text-zinc-100 dark:ring-white/10',
        className,
      )}
    >
      <RetailerMonogram retailer={retailer} className="size-4 text-[8px] ring-0" />
      <span className="truncate">{retailer.name}</span>
    </span>
  );
}
