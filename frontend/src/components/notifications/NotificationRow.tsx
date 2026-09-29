import { AlertCircle, BellOff, CheckCheck, Send, Trash2 } from 'lucide-react';
import type { Notification } from '@/lib/types';
import { cn } from '@/lib/utils';
import { ItemImage } from '../ItemImage';
import { RelativeTime } from '../RelativeTime';
import { Tooltip } from '../ui/tooltip';

export function DeliveryStatus({ n, compact }: { n: Notification; compact?: boolean }) {
  if (n.delivered) {
    return (
      <Tooltip content="Delivered via ntfy">
        <span className="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
          <Send className="size-3" />
          {!compact && 'Sent'}
        </span>
      </Tooltip>
    );
  }
  if (n.delivery_error) {
    return (
      <Tooltip content={`ntfy delivery failed: ${n.delivery_error}`}>
        <span className="inline-flex cursor-help items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
          <AlertCircle className="size-3" />
          {!compact && 'Failed'}
        </span>
      </Tooltip>
    );
  }
  return (
    <Tooltip content="Not pushed (ntfy not configured or notifications off for this item)">
      <span className="inline-flex items-center gap-1 text-xs text-zinc-400 dark:text-zinc-500">
        <BellOff className="size-3" />
        {!compact && 'Not sent'}
      </span>
    </Tooltip>
  );
}

export function NotificationRow({
  n,
  onOpen,
  onDelete,
  onMarkRead,
  compact,
}: {
  n: Notification;
  onOpen: (n: Notification) => void;
  onDelete?: (n: Notification) => void;
  onMarkRead?: (n: Notification) => void;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        'group relative flex gap-3 transition-colors',
        compact ? 'px-3 py-2.5' : 'px-4 py-3.5 sm:px-5',
        'hover:bg-zinc-50 dark:hover:bg-zinc-800/40',
        !n.read && 'bg-indigo-50/40 dark:bg-indigo-500/[0.04]',
      )}
    >
      <button
        type="button"
        onClick={() => onOpen(n)}
        className="absolute inset-0 z-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-indigo-500/60"
        aria-label={`Open notification: ${n.title}`}
      />
      <div className="relative shrink-0">
        <ItemImage
          src={n.image_url}
          alt=""
          className={cn('rounded-lg ring-1 ring-zinc-200 dark:ring-zinc-800', compact ? 'size-10' : 'size-12')}
          iconClassName="size-5"
        />
        {!n.read && (
          <span className="absolute -left-1 -top-1 size-2.5 rounded-full bg-indigo-500 ring-2 ring-white dark:ring-zinc-900" />
        )}
      </div>
      <div className="pointer-events-none relative z-[1] min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <p
            className={cn(
              'truncate text-sm',
              n.read ? 'font-medium text-zinc-700 dark:text-zinc-300' : 'font-semibold text-zinc-900 dark:text-zinc-50',
            )}
          >
            {n.title}
          </p>
          <RelativeTime
            iso={n.created_at}
            className="pointer-events-auto shrink-0 text-xs text-zinc-400 dark:text-zinc-500"
          />
        </div>
        <p className={cn('mt-0.5 text-sm text-zinc-500 dark:text-zinc-400', compact ? 'line-clamp-2' : 'line-clamp-3')}>
          {n.message}
        </p>
        {!compact && (
          <div className="pointer-events-auto mt-2 flex items-center gap-3">
            {n.item_name && <span className="truncate text-xs text-zinc-400 dark:text-zinc-500">{n.item_name}</span>}
            <DeliveryStatus n={n} />
          </div>
        )}
      </div>
      {!compact && (onDelete || onMarkRead) && (
        <div className="relative z-[1] flex shrink-0 items-start gap-0.5 opacity-100 transition-opacity sm:opacity-0 sm:group-focus-within:opacity-100 sm:group-hover:opacity-100">
          {onMarkRead && !n.read && (
            <Tooltip content="Mark as read">
              <button
                type="button"
                onClick={() => onMarkRead(n)}
                className="rounded-md p-1.5 text-zinc-400 hover:bg-zinc-200/70 hover:text-zinc-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
                aria-label="Mark as read"
              >
                <CheckCheck className="size-4" />
              </button>
            </Tooltip>
          )}
          {onDelete && (
            <Tooltip content="Delete">
              <button
                type="button"
                onClick={() => onDelete(n)}
                className="rounded-md p-1.5 text-zinc-400 hover:bg-rose-50 hover:text-rose-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 dark:hover:bg-rose-500/10 dark:hover:text-rose-400"
                aria-label="Delete notification"
              >
                <Trash2 className="size-4" />
              </button>
            </Tooltip>
          )}
        </div>
      )}
    </div>
  );
}
