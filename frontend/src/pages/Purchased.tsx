import { useQuery } from '@tanstack/react-query';
import { ShoppingBag, Undo2 } from 'lucide-react';
import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { ItemImage } from '@/components/ItemImage';
import { ItemActionsMenu } from '@/components/items/ItemActionsMenu';
import { AppleLogo } from '@/components/Logo';
import { PageHeader } from '@/components/PageHeader';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Skeleton } from '@/components/ui/skeleton';
import { useItemActions } from '@/hooks/useItemActions';
import { api } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import type { Item } from '@/lib/types';
import { absoluteTime, hostOf, parseDate } from '@/lib/utils';

function purchasedOn(iso: string | null | undefined) {
  const d = parseDate(iso);
  return d ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : '';
}

function PurchasedRow({ item }: { item: Item }) {
  const a = useItemActions();
  return (
    <div className="group relative flex items-center gap-3 px-3 py-3 transition-colors hover:bg-zinc-50 sm:gap-4 sm:px-4 dark:hover:bg-zinc-800/30">
      <Link to={`/items/${item.id}`} className="absolute inset-0 z-0 focus-visible:outline-none" aria-label={item.name} />
      <ItemImage src={item.image_url} alt="" className="pointer-events-none size-14 shrink-0 rounded-lg ring-1 ring-zinc-200 dark:ring-zinc-800" iconClassName="size-5" />
      <div className="pointer-events-none relative min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h3 className="truncate text-sm font-semibold text-zinc-900 dark:text-zinc-50">{item.name}</h3>
          {item.kind === 'apple' && <AppleLogo className="size-3 shrink-0 text-zinc-400" />}
        </div>
        <p className="mt-0.5 truncate text-xs text-zinc-500 dark:text-zinc-400" title={absoluteTime(item.purchased_at)}>
          {hostOf(item.url)}
          <span className="mx-1.5 text-zinc-300 dark:text-zinc-700">·</span>
          Purchased {purchasedOn(item.purchased_at)}
        </p>
      </div>
      <div className="pointer-events-none relative hidden w-24 shrink-0 text-right text-sm font-medium tabular-nums text-zinc-900 sm:block dark:text-zinc-100">
        {item.purchased_price ?? <span className="text-zinc-400">—</span>}
      </div>
      <Button
        variant="outline"
        size="sm"
        className="relative z-10 shrink-0"
        loading={a.unpurchase.isPending && a.unpurchase.variables?.id === item.id}
        onClick={() => a.unpurchase.mutate(item)}
      >
        <Undo2 /> <span className="hidden sm:inline">Watch again</span>
      </Button>
      <div className="relative z-10">
        <ItemActionsMenu item={item} />
      </div>
    </div>
  );
}

export default function Purchased() {
  const items = useQuery({ queryKey: qk.items, queryFn: api.items });
  const purchased = useMemo(
    () =>
      (items.data ?? [])
        .filter((it) => it.purchased_at)
        .sort((x, y) => (parseDate(y.purchased_at)?.getTime() ?? 0) - (parseDate(x.purchased_at)?.getTime() ?? 0)),
    [items.data],
  );

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader title="Purchased" description="Things you've bought. They're no longer checked or alerted on." />
      {items.isLoading ? (
        <Card className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} className="flex items-center gap-4 p-4">
              <Skeleton className="size-14 rounded-lg" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="h-3 w-1/3" />
              </div>
            </div>
          ))}
        </Card>
      ) : purchased.length === 0 ? (
        <EmptyState
          icon={<ShoppingBag />}
          title="Nothing purchased yet"
          description='When you buy something you were watching, hit "Bought it" (or Mark purchased in its menu) and it moves here.'
        />
      ) : (
        <Card className="divide-y divide-zinc-100 overflow-hidden dark:divide-zinc-800">
          {purchased.map((it) => (
            <PurchasedRow key={it.id} item={it} />
          ))}
        </Card>
      )}
    </div>
  );
}
