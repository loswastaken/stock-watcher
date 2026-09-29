import { useIsMutating, useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { useConfirm } from '@/components/ui/confirm';
import { statusMeta } from '@/components/StatusBadge';
import { api, errorMessage } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import type { Item } from '@/lib/types';

export function upsertItem(qc: QueryClient, item: Item) {
  qc.setQueryData<Item>(qk.item(item.id), item);
  qc.setQueryData<Item[]>(qk.items, (list) => {
    if (!list) return list;
    const i = list.findIndex((x) => x.id === item.id);
    if (i === -1) return [...list, item];
    const copy = list.slice();
    copy[i] = item;
    return copy;
  });
}

const CHECK_KEY = ['check-item'];

/** True while a "Check now" for this item is in flight (from any component). */
export function useIsChecking(id: number): boolean {
  return (
    useIsMutating({
      mutationKey: CHECK_KEY,
      predicate: (m) => (m.state.variables as Item | undefined)?.id === id,
    }) > 0
  );
}

export function useItemActions() {
  const qc = useQueryClient();
  const confirm = useConfirm();

  const after = (item?: Item) => {
    if (item) upsertItem(qc, item);
    qc.invalidateQueries({ queryKey: qk.stats });
  };

  const check = useMutation({
    mutationKey: CHECK_KEY,
    mutationFn: (item: Item) => api.checkItem(item.id),
    onSuccess: (item) => {
      after(item);
      qc.invalidateQueries({ queryKey: qk.history(item.id) });
      qc.invalidateQueries({ queryKey: qk.unread });
      const m = statusMeta[item.status] ?? statusMeta.unknown;
      const text = item.status === 'error' ? item.last_error || item.status_text : item.status_text;
      const fn = item.status === 'in_stock' ? toast.success : item.status === 'error' ? toast.warning : toast.info;
      fn(`${m.label}: ${item.name}`, { description: text || undefined });
    },
    onError: (e) => toast.error('Check failed', { description: errorMessage(e) }),
  });

  const toggle = useMutation({
    mutationFn: (item: Item) => api.updateItem(item.id, { enabled: !item.enabled }),
    onMutate: (item) => {
      upsertItem(qc, { ...item, enabled: !item.enabled });
    },
    onSuccess: (item) => {
      after(item);
      toast.success(item.enabled ? 'Watching resumed' : 'Watching paused', { description: item.name });
    },
    onError: (e, item) => {
      upsertItem(qc, item);
      toast.error(errorMessage(e));
    },
  });

  const toggleNotify = useMutation({
    mutationFn: (item: Item) => api.updateItem(item.id, { notify_enabled: !item.notify_enabled }),
    onSuccess: (item) => {
      after(item);
      toast.success(item.notify_enabled ? 'Alerts re-armed' : 'Alerts paused', {
        description: item.notify_enabled ? `You'll get one alert the next time ${item.name} is in stock.` : item.name,
      });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const refreshImage = useMutation({
    mutationFn: (item: Item) => api.refreshImage(item.id),
    onSuccess: (item) => {
      after(item);
      toast.success('Photo refreshed');
    },
    onError: (e) => toast.error('Could not refresh photo', { description: errorMessage(e) }),
  });

  const remove = useMutation({
    mutationFn: (item: Item) => api.deleteItem(item.id),
    onSuccess: (_d, item) => {
      qc.setQueryData<Item[]>(qk.items, (list) => list?.filter((x) => x.id !== item.id));
      qc.removeQueries({ queryKey: qk.item(item.id), exact: true });
      qc.removeQueries({ queryKey: qk.history(item.id) });
      qc.invalidateQueries({ queryKey: qk.stats });
      toast.success('Item deleted', { description: item.name });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const confirmDelete = async (item: Item): Promise<boolean> => {
    const ok = await confirm({
      title: 'Delete this item?',
      description: `"${item.name}" and its check history will be permanently removed.`,
      confirmText: 'Delete',
      destructive: true,
    });
    if (!ok) return false;
    try {
      await remove.mutateAsync(item);
      return true;
    } catch {
      return false;
    }
  };

  return { check, toggle, toggleNotify, refreshImage, remove, confirmDelete };
}
