import { useInfiniteQuery } from '@tanstack/react-query';
import { AlertTriangle, BellOff, CheckCheck, Inbox, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { NotificationRow } from '@/components/notifications/NotificationRow';
import { useNotificationActions } from '@/components/notifications/useNotificationActions';
import { PageHeader } from '@/components/PageHeader';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useConfirm } from '@/components/ui/confirm';
import { EmptyState } from '@/components/ui/empty-state';
import { Segmented } from '@/components/ui/segmented';
import { Skeleton } from '@/components/ui/skeleton';
import { api, errorMessage } from '@/lib/api';
import { qk } from '@/lib/queryClient';

const PAGE = 30;

export default function Notifications() {
  const [filter, setFilter] = useState<'all' | 'unread'>('all');
  const confirm = useConfirm();
  const actions = useNotificationActions();
  const q = useInfiniteQuery({
    queryKey: [...qk.notifications, 'list', filter],
    queryFn: ({ pageParam }) => api.notifications({ unread_only: filter === 'unread', limit: PAGE, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, p) => n + p.items.length, 0);
      return loaded < last.total && last.items.length > 0 ? loaded : undefined;
    },
    refetchInterval: 30_000,
  });

  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  const first = q.data?.pages[0];
  const unread = first?.unread_count ?? 0;
  const total = first?.total ?? 0;

  const clearAll = async () => {
    const ok = await confirm({
      title: 'Clear all notifications?',
      description: 'This permanently deletes every notification in your notification center.',
      confirmText: 'Clear all',
      destructive: true,
    });
    if (ok) actions.clearAll.mutate();
  };

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        title="Notifications"
        description={first ? (unread ? `${unread} unread of ${total}` : `${total} notification${total === 1 ? '' : 's'}`) : 'Restock alerts and status changes.'}
        actions={
          <>
            <Button size="sm" onClick={() => actions.markAllRead.mutate()} disabled={!unread} loading={actions.markAllRead.isPending}>
              {!actions.markAllRead.isPending && <CheckCheck />} Mark all read
            </Button>
            <Button size="sm" variant="ghost" onClick={clearAll} disabled={!total} className="text-rose-600 hover:bg-rose-50 hover:text-rose-700 dark:text-rose-400 dark:hover:bg-rose-500/10 dark:hover:text-rose-300">
              <Trash2 /> Clear all
            </Button>
          </>
        }
      />

      <div className="mb-4">
        <Segmented
          ariaLabel="Filter notifications"
          value={filter}
          onChange={setFilter}
          options={[
            { value: 'all', label: 'All' },
            {
              value: 'unread',
              label: (
                <span className="inline-flex items-center gap-1.5">
                  Unread
                  {unread > 0 && (
                    <span className="rounded-full bg-indigo-600 px-1.5 text-[10px] font-semibold leading-4 text-white dark:bg-indigo-500">{unread}</span>
                  )}
                </span>
              ),
            },
          ]}
        />
      </div>

      {q.isError ? (
        <EmptyState
          icon={<AlertTriangle />}
          title="Couldn't load notifications"
          description={errorMessage(q.error)}
          action={<Button onClick={() => q.refetch()}>Retry</Button>}
        />
      ) : q.isLoading ? (
        <Card className="divide-y divide-zinc-100 dark:divide-zinc-800/80">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="flex gap-3 px-5 py-4">
              <Skeleton className="size-12 rounded-lg" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="h-3 w-5/6" />
                <Skeleton className="h-3 w-24" />
              </div>
            </div>
          ))}
        </Card>
      ) : !items.length ? (
        <EmptyState
          icon={filter === 'unread' ? <Inbox /> : <BellOff />}
          title={filter === 'unread' ? "You're all caught up" : 'No notifications yet'}
          description={
            filter === 'unread'
              ? 'No unread notifications.'
              : "When something you're watching comes back in stock, you'll see it here and on your phone via ntfy."
          }
        />
      ) : (
        <>
          <Card className="divide-y divide-zinc-100 overflow-hidden dark:divide-zinc-800/80">
            {items.map((n) => (
              <NotificationRow
                key={n.id}
                n={n}
                onOpen={actions.open}
                onMarkRead={(x) => actions.markRead.mutate(x.id)}
                onDelete={(x) => actions.remove.mutate(x.id)}
              />
            ))}
          </Card>
          <div className="mt-4 flex justify-center">
            {q.hasNextPage ? (
              <Button onClick={() => q.fetchNextPage()} loading={q.isFetchingNextPage}>
                Load more
              </Button>
            ) : (
              items.length > PAGE && <p className="text-xs text-zinc-400">That's everything.</p>
            )}
          </div>
        </>
      )}
    </div>
  );
}
