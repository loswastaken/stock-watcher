import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { api } from '@/lib/api';
import { qk } from '@/lib/queryClient';

/** Polls unread notifications (latest 5) every 30 s. Shared by bell + mobile tab bar. */
export function useUnread() {
  return useQuery({
    queryKey: qk.unread,
    queryFn: () => api.notifications({ unread_only: true, limit: 5 }),
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
}

/** Mount once: toasts when the unread count grows while the app is open. */
export function useUnreadWatcher() {
  const { data } = useUnread();
  const prev = useRef<number | null>(null);
  const seen = useRef<Set<number>>(new Set());
  const qc = useQueryClient();
  const navigate = useNavigate();

  useEffect(() => {
    if (!data) return;
    const count = data.unread_count;
    if (prev.current === null) {
      data.items.forEach((n) => seen.current.add(n.id));
      prev.current = count;
      return;
    }
    if (count > prev.current) {
      const fresh = data.items.filter((n) => !seen.current.has(n.id));
      const toShow = fresh.slice(0, 3);
      toShow.forEach((n) => {
        toast(n.title, {
          description: n.message,
          duration: 8000,
          action: {
            label: 'View',
            onClick: () => navigate(n.item_id != null ? `/items/${n.item_id}` : '/notifications'),
          },
        });
      });
      if (!toShow.length) {
        toast(`${count - prev.current} new alert${count - prev.current > 1 ? 's' : ''}`, {
          action: { label: 'View', onClick: () => navigate('/notifications') },
        });
      }
      // A new alert usually means item state changed.
      qc.invalidateQueries({ queryKey: qk.items });
      qc.invalidateQueries({ queryKey: qk.stats });
      qc.invalidateQueries({ queryKey: qk.notifications, predicate: (q) => q.queryKey[1] !== 'unread-peek' });
    }
    data.items.forEach((n) => seen.current.add(n.id));
    prev.current = count;
  }, [data, qc, navigate]);
}
