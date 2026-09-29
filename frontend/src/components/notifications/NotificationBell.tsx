import { useQuery } from '@tanstack/react-query';
import { Bell, BellRing, CheckCheck } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import { cn } from '@/lib/utils';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { Skeleton } from '../ui/skeleton';
import { NotificationRow } from './NotificationRow';
import { useNotificationActions } from './useNotificationActions';
import { useUnread } from './useUnread';

export function UnreadBadge({ count, className }: { count: number; className?: string }) {
  if (!count) return null;
  return (
    <span
      className={cn(
        'flex h-4 min-w-4 items-center justify-center rounded-full bg-rose-500 px-1 text-[10px] font-semibold leading-none text-white ring-2 ring-white dark:ring-zinc-950',
        className,
      )}
    >
      {count > 99 ? '99+' : count}
    </span>
  );
}

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const { data: unread } = useUnread();
  const count = unread?.unread_count ?? 0;
  const latest = useQuery({
    queryKey: [...qk.notifications, 'latest'],
    queryFn: () => api.notifications({ limit: 5 }),
    enabled: open,
  });
  const actions = useNotificationActions();

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="relative rounded-lg p-2 text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 data-[state=open]:bg-zinc-100 data-[state=open]:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800/70 dark:hover:text-zinc-100 dark:data-[state=open]:bg-zinc-800/70 dark:data-[state=open]:text-zinc-100"
          aria-label={count ? `Notifications (${count} unread)` : 'Notifications'}
        >
          {count ? <BellRing className="size-5" /> : <Bell className="size-5" />}
          <UnreadBadge count={count} className="absolute right-0.5 top-0.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-[min(24rem,calc(100vw-1rem))] overflow-hidden p-0">
        <div className="flex items-center justify-between border-b border-zinc-100 px-4 py-3 dark:border-zinc-800">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-50">Notifications</h2>
            {count > 0 && (
              <span className="rounded-full bg-indigo-100 px-1.5 py-0.5 text-[11px] font-medium text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300">
                {count} new
              </span>
            )}
          </div>
          {count > 0 && (
            <button
              type="button"
              onClick={() => actions.markAllRead.mutate()}
              className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs font-medium text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            >
              <CheckCheck className="size-3.5" /> Mark all read
            </button>
          )}
        </div>
        <div className="max-h-[min(24rem,60dvh)] divide-y divide-zinc-100 overflow-y-auto dark:divide-zinc-800/80">
          {latest.isLoading ? (
            Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="flex gap-3 px-3 py-3">
                <Skeleton className="size-10 rounded-lg" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-3.5 w-2/3" />
                  <Skeleton className="h-3 w-full" />
                </div>
              </div>
            ))
          ) : latest.data?.items.length ? (
            latest.data.items.map((n) => (
              <NotificationRow
                key={n.id}
                n={n}
                compact
                onOpen={(x) => {
                  setOpen(false);
                  actions.open(x);
                }}
              />
            ))
          ) : (
            <div className="flex flex-col items-center gap-2 px-6 py-10 text-center">
              <Bell className="size-6 text-zinc-300 dark:text-zinc-700" />
              <p className="text-sm text-zinc-500 dark:text-zinc-400">No notifications yet</p>
              <p className="text-xs text-zinc-400 dark:text-zinc-500">Restock alerts will show up here.</p>
            </div>
          )}
        </div>
        <Link
          to="/notifications"
          onClick={() => setOpen(false)}
          className="block border-t border-zinc-100 px-4 py-2.5 text-center text-sm font-medium text-indigo-600 hover:bg-zinc-50 focus-visible:bg-zinc-50 focus-visible:outline-none dark:border-zinc-800 dark:text-indigo-400 dark:hover:bg-zinc-800/50"
        >
          View all notifications
        </Link>
      </PopoverContent>
    </Popover>
  );
}
