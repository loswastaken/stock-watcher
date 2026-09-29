import { AlertTriangle, CheckCircle2, CircleSlash, HelpCircle, PauseCircle } from 'lucide-react';
import type { ItemStatus } from '@/lib/types';
import { cn } from '@/lib/utils';

export const statusMeta: Record<
  ItemStatus,
  { label: string; dot: string; badge: string; text: string; bar: string }
> = {
  in_stock: {
    label: 'In stock',
    dot: 'bg-emerald-500',
    badge:
      'bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-400 dark:ring-emerald-500/30',
    text: 'text-emerald-600 dark:text-emerald-400',
    bar: 'bg-emerald-500',
  },
  out_of_stock: {
    label: 'Out of stock',
    dot: 'bg-rose-500',
    badge: 'bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-500/10 dark:text-rose-400 dark:ring-rose-500/30',
    text: 'text-rose-600 dark:text-rose-400',
    bar: 'bg-rose-500/80',
  },
  unknown: {
    label: 'Unknown',
    dot: 'bg-zinc-400',
    badge: 'bg-zinc-100 text-zinc-600 ring-zinc-500/20 dark:bg-zinc-800 dark:text-zinc-400 dark:ring-zinc-700',
    text: 'text-zinc-500 dark:text-zinc-400',
    bar: 'bg-zinc-400 dark:bg-zinc-600',
  },
  error: {
    label: 'Error',
    dot: 'bg-amber-500',
    badge: 'bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-400 dark:ring-amber-500/30',
    text: 'text-amber-600 dark:text-amber-400',
    bar: 'bg-amber-500',
  },
};

const icons: Record<ItemStatus, typeof CheckCircle2> = {
  in_stock: CheckCircle2,
  out_of_stock: CircleSlash,
  unknown: HelpCircle,
  error: AlertTriangle,
};

export function StatusDot({ status, pulse, className }: { status: ItemStatus; pulse?: boolean; className?: string }) {
  const m = statusMeta[status] ?? statusMeta.unknown;
  return (
    <span className={cn('relative inline-flex size-2 shrink-0', className)}>
      {pulse && status === 'in_stock' && <span className={cn('absolute inset-0 animate-ping-slow rounded-full', m.dot)} />}
      <span className={cn('relative inline-flex size-2 rounded-full', m.dot)} />
    </span>
  );
}

export function StatusBadge({
  status,
  paused,
  className,
  size = 'md',
  overlay,
}: {
  status: ItemStatus;
  paused?: boolean;
  className?: string;
  size?: 'sm' | 'md';
  /** Solid style for placing on top of photos. */
  overlay?: boolean;
}) {
  if (overlay) {
    const m = statusMeta[status] ?? statusMeta.unknown;
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-white/95 font-medium text-zinc-800 shadow-sm ring-1 ring-zinc-900/10 backdrop-blur dark:bg-zinc-900/90 dark:text-zinc-100 dark:ring-white/10',
          size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-0.5 text-xs',
          className,
        )}
      >
        <span className={cn('size-1.5 rounded-full', paused ? 'bg-zinc-400' : m.dot)} />
        {paused ? 'Paused' : m.label}
      </span>
    );
  }
  if (paused) {
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1 whitespace-nowrap rounded-full bg-zinc-100 font-medium text-zinc-600 ring-1 ring-inset ring-zinc-500/20 dark:bg-zinc-800 dark:text-zinc-400 dark:ring-zinc-700',
          size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-0.5 text-xs',
          className,
        )}
      >
        <PauseCircle className="size-3.5" /> Paused
      </span>
    );
  }
  const m = statusMeta[status] ?? statusMeta.unknown;
  const Icon = icons[status] ?? HelpCircle;
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-full font-medium ring-1 ring-inset',
        size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-0.5 text-xs',
        m.badge,
        className,
      )}
    >
      <Icon className="size-3.5" />
      {m.label}
    </span>
  );
}
