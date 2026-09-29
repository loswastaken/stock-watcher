import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Globe, Plus, ShoppingCart, Store, Zap } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { RelativeTime } from '@/components/RelativeTime';
import { RetailerMonogram } from '@/components/RetailerBadge';
import { StatusBadge } from '@/components/StatusBadge';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { upsertItem } from '@/hooks/useItemActions';
import { api, errorMessage } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import type { Item, StoreRow } from '@/lib/types';
import { absoluteTime, cn, hostOf, isAboveLimit, isAppleUrl, isValidUrl } from '@/lib/utils';

function StoreName({ row, current }: { row: StoreRow; current: boolean }) {
  const label = row.retailer?.name ?? hostOf(row.url);
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      {row.retailer ? (
        <RetailerMonogram retailer={row.retailer} className="size-7 text-[10px]" />
      ) : (
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
          <Globe className="size-3.5" />
        </span>
      )}
      <div className="min-w-0">
        {current ? (
          <p className="truncate font-medium text-zinc-900 dark:text-zinc-100">
            {label} <span className="text-xs font-normal text-zinc-400">(this item)</span>
          </p>
        ) : (
          <Link to={`/items/${row.id}`} className="block truncate font-medium text-zinc-900 hover:text-indigo-600 dark:text-zinc-100 dark:hover:text-indigo-400">
            {label}
          </Link>
        )}
        <p className="truncate text-xs text-zinc-500 dark:text-zinc-400">{row.status_text || hostOf(row.url)}</p>
      </div>
    </div>
  );
}

/** Same product at every store it's tracked at (product_group), plus "Track at another store". */
export function StoresPanel({ item }: { item: Item }) {
  const qc = useQueryClient();
  const stores = useQuery({ queryKey: qk.stores(item.id), queryFn: () => api.stores(item.id), refetchInterval: 30_000 });
  const [url, setUrl] = useState('');
  const [adding, setAdding] = useState(false);
  const trimmed = url.trim();
  const invalid = !!trimmed && !isValidUrl(trimmed);

  const add = useMutation({
    mutationFn: () => api.addStore(item.id, trimmed),
    onSuccess: (sib) => {
      upsertItem(qc, sib);
      qc.invalidateQueries({ queryKey: qk.items });
      qc.invalidateQueries({ queryKey: qk.item(item.id) });
      qc.invalidateQueries({ queryKey: ['stores'] });
      qc.invalidateQueries({ queryKey: qk.stats });
      toast.success(`Now tracking at ${sib.retailer?.name ?? hostOf(sib.url)}`, { description: sib.name });
      setUrl('');
      setAdding(false);
    },
    onError: (e) => toast.error("Couldn't add store", { description: errorMessage(e) }),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!trimmed || invalid) return;
    if (isAppleUrl(trimmed)) {
      toast.error('Add Apple Store products from the Add item page');
      return;
    }
    add.mutate();
  };

  const rows = stores.data ?? [];
  const inStock = rows.filter((r) => r.status === 'in_stock' && r.enabled).length;

  return (
    <Card>
      <CardHeader
        icon={<Store />}
        title="Stores"
        description={
          rows.length > 1
            ? `In stock at ${inStock} of ${rows.length} stores`
            : 'Track this product at other stores to see them side by side.'
        }
        actions={
          !adding && (
            <Button size="sm" onClick={() => setAdding(true)}>
              <Plus /> <span className="hidden sm:inline">Track at another store</span>
              <span className="sm:hidden">Add store</span>
            </Button>
          )
        }
      />
      {adding && (
        <form onSubmit={onSubmit} className="flex flex-col gap-2 border-b border-zinc-100 px-5 py-4 sm:flex-row dark:border-zinc-800/80" noValidate>
          <div className="flex-1">
            <Input
              autoFocus
              type="url"
              inputMode="url"
              placeholder="Paste the product link at another store"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              leading={<Globe />}
              invalid={invalid}
              aria-label="Product URL at another store"
            />
            {invalid && <p className="mt-1 text-xs text-rose-600 dark:text-rose-400">Enter a valid http(s) URL</p>}
          </div>
          <div className="flex gap-2">
            <Button type="submit" variant="primary" loading={add.isPending} disabled={!trimmed || invalid}>
              Track
            </Button>
            <Button variant="ghost" onClick={() => { setAdding(false); setUrl(''); }}>
              Cancel
            </Button>
          </div>
        </form>
      )}
      {stores.isLoading ? (
        <CardBody className="space-y-2">
          <Skeleton className="h-10 w-full" />
        </CardBody>
      ) : stores.isError ? (
        <CardBody>
          <p className="text-sm text-rose-600 dark:text-rose-400">{errorMessage(stores.error)}</p>
        </CardBody>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-100 text-left text-xs text-zinc-500 dark:border-zinc-800/80 dark:text-zinc-400">
                <th className="px-5 py-2.5 font-medium">Store</th>
                <th className="px-3 py-2.5 font-medium">Status</th>
                <th className="px-3 py-2.5 text-right font-medium">Price</th>
                <th className="hidden px-5 py-2.5 text-right font-medium sm:table-cell">Last in stock</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800/80">
              {rows.map((r) => (
                <tr key={r.id} className={cn(r.status === 'in_stock' && r.enabled && 'bg-emerald-50/60 dark:bg-emerald-500/[0.06]')}>
                  <td className="max-w-[14rem] px-5 py-3">
                    <StoreName row={r} current={r.id === item.id} />
                  </td>
                  <td className="px-3 py-3">
                    <StatusBadge status={r.status} paused={!r.enabled} size="sm" />
                  </td>
                  <td
                    className={cn(
                      'whitespace-nowrap px-3 py-3 text-right font-medium tabular-nums',
                      isAboveLimit(r) ? 'text-amber-700 dark:text-amber-400' : 'text-zinc-900 dark:text-zinc-100',
                    )}
                  >
                    {r.price ?? <span className="text-zinc-400">—</span>}
                  </td>
                  <td className="hidden whitespace-nowrap px-5 py-3 text-right text-xs text-zinc-500 sm:table-cell dark:text-zinc-400">
                    <RelativeTime iso={r.last_in_stock_at} fallback="—" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/** When the item came back in stock (status turned in_stock after being out of stock). */
export function RestockPanel({ item }: { item: Item }) {
  const restocks = useQuery({
    queryKey: qk.restocks(item.id),
    queryFn: () => api.restocks(item.id, 20),
    refetchInterval: 60_000,
  });
  const rows = restocks.data ?? [];
  return (
    <Card>
      <CardHeader
        icon={<Zap />}
        title="Restock history"
        description={rows.length ? `Last ${rows.length} restock${rows.length === 1 ? '' : 's'} we saw` : undefined}
      />
      <CardBody className={cn(rows.length > 0 && 'p-0')}>
        {restocks.isLoading ? (
          <Skeleton className="h-10 w-full" />
        ) : restocks.isError ? (
          <p className="text-sm text-rose-600 dark:text-rose-400">{errorMessage(restocks.error)}</p>
        ) : !rows.length ? (
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            {item.last_in_stock_at ? (
              <>
                No restocks recorded yet. Last seen in stock <RelativeTime iso={item.last_in_stock_at} />.
              </>
            ) : (
              'No restocks recorded yet. Restock times show up here so you can spot drop patterns.'
            )}
          </p>
        ) : (
          <ul className="divide-y divide-zinc-100 dark:divide-zinc-800/80">
            {rows.map((r) => (
              <li key={r.id} className="flex items-center justify-between gap-3 px-5 py-2.5">
                <div className="flex min-w-0 items-center gap-2.5">
                  <span className="size-2 shrink-0 rounded-full bg-emerald-500" />
                  <div className="min-w-0">
                    <p className="text-sm font-medium tabular-nums text-zinc-900 dark:text-zinc-100">{absoluteTime(r.checked_at)}</p>
                    {r.status_text && <p className="truncate text-xs text-zinc-500 dark:text-zinc-400">{r.status_text}</p>}
                  </div>
                </div>
                <RelativeTime iso={r.checked_at} className="shrink-0 text-xs text-zinc-400 dark:text-zinc-500" />
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}

/** Direct add-to-cart / checkout link (pill), when the store has one. */
export function AddToCartLink({ item, className }: { item: Item; className?: string }) {
  if (!item.cart_url || item.purchased_at) return null;
  return (
    <a
      href={item.cart_url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      className={cn(
        'pointer-events-auto relative z-10 inline-flex items-center gap-1.5 rounded-full bg-indigo-50 px-2.5 py-1 text-xs font-medium text-indigo-700 ring-1 ring-indigo-300/60 transition-colors hover:bg-indigo-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 dark:bg-indigo-500/10 dark:text-indigo-300 dark:ring-indigo-500/30 dark:hover:bg-indigo-500/20',
        className,
      )}
    >
      <ShoppingCart className="size-3.5" /> Add to cart
    </a>
  );
}
