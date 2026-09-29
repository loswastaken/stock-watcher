import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import type { Notification, NotificationList } from '@/lib/types';
import type { InfiniteData } from '@tanstack/react-query';

type Cached = NotificationList | InfiniteData<NotificationList> | undefined;

function patchCache(old: Cached, fn: (n: Notification) => Notification | null): Cached {
  if (!old) return old;
  const patchList = (l: NotificationList): NotificationList => {
    let unread = l.unread_count;
    let total = l.total;
    const items: Notification[] = [];
    for (const n of l.items) {
      const next = fn(n);
      if (!next) {
        total -= 1;
        if (!n.read) unread -= 1;
        continue;
      }
      if (!n.read && next.read) unread -= 1;
      items.push(next);
    }
    return { items, unread_count: Math.max(0, unread), total: Math.max(0, total) };
  };
  if ('pages' in old) return { ...old, pages: old.pages.map(patchList) };
  return patchList(old);
}

export function useNotificationActions() {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const refresh = () => {
    qc.invalidateQueries({ queryKey: qk.notifications });
    qc.invalidateQueries({ queryKey: qk.stats });
  };

  const optimistic = (fn: (n: Notification) => Notification | null) => {
    qc.setQueriesData<Cached>({ queryKey: qk.notifications }, (old) => patchCache(old, fn));
  };

  const markRead = useMutation({
    mutationFn: (id: number) => api.markRead(id),
    onMutate: (id) => optimistic((n) => (n.id === id ? { ...n, read: true } : n)),
    onSettled: refresh,
  });

  const markAllRead = useMutation({
    mutationFn: api.markAllRead,
    onMutate: () => optimistic((n) => ({ ...n, read: true })),
    onSuccess: () => toast.success('All notifications marked as read'),
    onError: (e) => toast.error(errorMessage(e)),
    onSettled: refresh,
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteNotification(id),
    onMutate: (id) => optimistic((n) => (n.id === id ? null : n)),
    onError: (e) => toast.error(errorMessage(e)),
    onSettled: refresh,
  });

  const clearAll = useMutation({
    mutationFn: api.clearNotifications,
    onSuccess: () => toast.success('Notifications cleared'),
    onError: (e) => toast.error(errorMessage(e)),
    onSettled: refresh,
  });

  const open = (n: Notification) => {
    if (!n.read) markRead.mutate(n.id);
    if (n.item_id != null) navigate(`/items/${n.item_id}`);
    else navigate('/notifications');
  };

  return { markRead, markAllRead, remove, clearAll, open };
}
