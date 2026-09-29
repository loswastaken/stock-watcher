import { BellOff, Loader2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useIsChecking } from '@/hooks/useItemActions';
import type { Item } from '@/lib/types';
import { cn, hostOf } from '@/lib/utils';
import { ItemImage } from '../ItemImage';
import { AppleLogo } from '../Logo';
import { RelativeTime } from '../RelativeTime';
import { StatusBadge, StatusDot, statusMeta } from '../StatusBadge';
import { Tooltip } from '../ui/tooltip';
import { ItemActionsMenu } from './ItemActionsMenu';

export function AppleBadge({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full bg-white/90 px-2 py-0.5 text-[11px] font-medium text-zinc-800 shadow-sm ring-1 ring-zinc-900/5 backdrop-blur dark:bg-zinc-800/90 dark:text-zinc-100 dark:ring-white/10',
        className,
      )}
    >
      <AppleLogo className="size-3 -translate-y-px" /> Apple
    </span>
  );
}

function statusLine(item: Item) {
  if (item.status === 'error') return item.last_error || item.status_text || 'Check failed';
  return item.status_text || statusMeta[item.status]?.label || '';
}

export function ItemCard({ item }: { item: Item }) {
  const checking = useIsChecking(item.id);
  const paused = !item.enabled;
  return (
    <div
      className={cn(
        'group relative flex flex-col overflow-hidden rounded-xl border bg-white shadow-soft transition-all duration-200',
        'hover:-translate-y-0.5 hover:shadow-lifted focus-within:ring-2 focus-within:ring-indigo-500/40',
        'dark:bg-zinc-900/50',
        item.status === 'in_stock' && !paused
          ? 'border-emerald-300/70 dark:border-emerald-500/30'
          : 'border-zinc-200 hover:border-zinc-300 dark:border-zinc-800/80 dark:hover:border-zinc-700',
      )}
    >
      <Link to={`/items/${item.id}`} className="absolute inset-0 z-0 focus-visible:outline-none" aria-label={item.name} />
      <div className="pointer-events-none relative">
        <ItemImage
          src={item.image_url}
          alt={item.name}
          className={cn('aspect-[4/3] w-full', paused && 'opacity-50 grayscale')}
          imgClassName="transition-transform duration-300 group-hover:scale-[1.03]"
        />
        <div className="absolute left-2 top-2 flex items-center gap-1.5 sm:left-3 sm:top-3">
          <StatusBadge status={item.status} paused={paused} size="sm" overlay />
        </div>
        {item.kind === 'apple' && <AppleBadge className="absolute bottom-2 left-2 sm:bottom-3 sm:left-3" />}
        {checking && (
          <div className="absolute inset-0 flex items-center justify-center bg-white/50 backdrop-blur-[1px] dark:bg-zinc-950/50">
            <span className="inline-flex items-center gap-2 rounded-full bg-white px-3 py-1 text-xs font-medium text-zinc-700 shadow ring-1 ring-zinc-200 dark:bg-zinc-900 dark:text-zinc-200 dark:ring-zinc-700">
              <Loader2 className="size-3.5 animate-spin" /> Checking…
            </span>
          </div>
        )}
      </div>
      <div className="absolute right-2 top-2 z-10">
        <ItemActionsMenu
          item={item}
          className="bg-white/80 shadow-sm ring-1 ring-zinc-900/5 backdrop-blur hover:bg-white dark:bg-zinc-900/80 dark:ring-white/10 dark:hover:bg-zinc-800 sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100 sm:data-[state=open]:opacity-100"
        />
      </div>
      <div className="pointer-events-none relative flex flex-1 flex-col gap-2 border-t border-zinc-100 p-3 sm:p-4 dark:border-zinc-800/80">
        <div className="min-w-0">
          <h3 className="line-clamp-2 text-sm font-semibold leading-snug text-zinc-900 dark:text-zinc-50">{item.name}</h3>
          <p className="mt-1 flex items-center gap-1.5 truncate text-xs text-zinc-500 dark:text-zinc-400">
            <span className="truncate">{hostOf(item.url)}</span>
            {!item.notify_enabled && (
              <Tooltip content="Notifications muted">
                <BellOff className="pointer-events-auto size-3 shrink-0" />
              </Tooltip>
            )}
          </p>
        </div>
        <div className="mt-auto flex flex-col-reverse gap-1 pt-1 min-[480px]:flex-row min-[480px]:items-end min-[480px]:justify-between min-[480px]:gap-2">
          <div className="min-w-0">
            <p className={cn('truncate text-xs font-medium', paused ? 'text-zinc-500' : statusMeta[item.status]?.text)}>
              {statusLine(item)}
            </p>
            <p className="mt-0.5 text-xs text-zinc-400 dark:text-zinc-500">
              <RelativeTime iso={item.last_checked_at} prefix="Checked" fallback="not yet" className="pointer-events-auto" />
            </p>
          </div>
          {item.price && (
            <span className="shrink-0 text-sm font-semibold tabular-nums text-zinc-900 dark:text-zinc-100">{item.price}</span>
          )}
        </div>
      </div>
    </div>
  );
}

export function ItemRow({ item }: { item: Item }) {
  const checking = useIsChecking(item.id);
  const paused = !item.enabled;
  return (
    <div className="group relative flex items-center gap-3 px-3 py-3 transition-colors hover:bg-zinc-50 focus-within:bg-zinc-50 sm:gap-4 sm:px-4 dark:hover:bg-zinc-800/30 dark:focus-within:bg-zinc-800/30">
      <Link to={`/items/${item.id}`} className="absolute inset-0 z-0 focus-visible:outline-none" aria-label={item.name} />
      <div className="pointer-events-none relative shrink-0">
        <ItemImage
          src={item.image_url}
          alt=""
          className={cn('size-14 rounded-lg ring-1 ring-zinc-200 dark:ring-zinc-800', paused && 'opacity-50 grayscale')}
          iconClassName="size-5"
        />
        {checking && (
          <div className="absolute inset-0 flex items-center justify-center rounded-lg bg-white/60 dark:bg-zinc-950/60">
            <Loader2 className="size-4 animate-spin text-zinc-500" />
          </div>
        )}
      </div>
      <div className="pointer-events-none relative min-w-0 flex-1">
        <div className="flex items-center gap-2">
          {!paused && <StatusDot status={item.status} pulse />}
          <h3 className="truncate text-sm font-semibold text-zinc-900 dark:text-zinc-50">{item.name}</h3>
          {item.kind === 'apple' && <AppleLogo className="size-3 shrink-0 text-zinc-400" />}
        </div>
        <p className="mt-0.5 truncate text-xs text-zinc-500 dark:text-zinc-400">
          {hostOf(item.url)}
          <span className="mx-1.5 text-zinc-300 dark:text-zinc-700">·</span>
          <span className={cn(paused ? '' : statusMeta[item.status]?.text)}>{statusLine(item)}</span>
        </p>
      </div>
      <div className="pointer-events-none relative hidden w-28 shrink-0 justify-end md:flex">
        <StatusBadge status={item.status} paused={paused} size="sm" />
      </div>
      <div className="pointer-events-none relative hidden w-24 shrink-0 text-right text-sm font-medium tabular-nums text-zinc-900 sm:block dark:text-zinc-100">
        {item.price ?? <span className="text-zinc-400">—</span>}
      </div>
      <div className="pointer-events-none relative hidden w-24 shrink-0 text-right text-xs text-zinc-500 lg:block">
        <RelativeTime iso={item.last_checked_at} fallback="not yet" className="pointer-events-auto" />
      </div>
      <div className="relative z-10">
        <ItemActionsMenu item={item} />
      </div>
    </div>
  );
}
