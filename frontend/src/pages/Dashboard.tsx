import { useQuery } from '@tanstack/react-query';
import {
  AlertTriangle,
  ArrowDownUp,
  BellRing,
  CheckCircle2,
  CircleSlash,
  Eye,
  LayoutGrid,
  List,
  PackageSearch,
  Plus,
  Search,
  X,
} from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ItemCard, ItemRow } from '@/components/items/ItemCard';
import { ItemCardSkeleton, ItemRowSkeleton } from '@/components/items/ItemSkeletons';
import { PageHeader } from '@/components/PageHeader';
import { Button, buttonClasses } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Input, Select } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Tooltip } from '@/components/ui/tooltip';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { api, errorMessage } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import type { Item, ItemStatus, Stats } from '@/lib/types';
import { cn, hostOf, parseDate } from '@/lib/utils';

type Filter = 'all' | 'in_stock' | 'out_of_stock' | 'error' | 'paused';
type Sort = 'name' | 'status' | 'change';

const FILTERS: { id: Filter; label: string; dot?: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'in_stock', label: 'In stock', dot: 'bg-emerald-500' },
  { id: 'out_of_stock', label: 'Out of stock', dot: 'bg-rose-500' },
  { id: 'error', label: 'Errors', dot: 'bg-amber-500' },
  { id: 'paused', label: 'Paused', dot: 'bg-zinc-400' },
];

const STATUS_ORDER: Record<ItemStatus, number> = { in_stock: 0, error: 1, unknown: 2, out_of_stock: 3 };

function matches(item: Item, f: Filter) {
  switch (f) {
    case 'all':
      return true;
    case 'paused':
      return !item.enabled;
    default:
      // Matches /stats semantics: status counts include paused items.
      return item.status === f;
  }
}

function StatCard({
  label,
  value,
  sub,
  icon,
  tone,
  loading,
  onClick,
  active,
}: {
  label: string;
  value: number | undefined;
  sub?: ReactNode;
  icon: ReactNode;
  tone: string;
  loading?: boolean;
  onClick?: () => void;
  active?: boolean;
}) {
  const Comp = onClick ? 'button' : 'div';
  return (
    <Comp
      type={onClick ? 'button' : undefined}
      onClick={onClick}
      className={cn(
        'group relative w-full overflow-hidden rounded-xl border bg-white p-3.5 text-left shadow-soft transition-all sm:p-5 dark:bg-zinc-900/40',
        active ? 'border-indigo-400/60 ring-1 ring-indigo-400/40 dark:border-indigo-500/40' : 'border-zinc-200 dark:border-zinc-800/80',
        onClick && 'hover:border-zinc-300 hover:shadow-lifted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 dark:hover:border-zinc-700',
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-zinc-500 sm:text-sm dark:text-zinc-400">{label}</span>
        <span className={cn('flex size-7 items-center justify-center rounded-lg [&_svg]:size-4', tone)}>{icon}</span>
      </div>
      <div className="mt-1 text-2xl font-semibold sm:mt-2 tabular-nums tracking-tight text-zinc-900 sm:text-3xl dark:text-zinc-50">
        {loading || value === undefined ? <Skeleton className="h-8 w-12" /> : value}
      </div>
      {sub && <div className="mt-1 truncate text-xs text-zinc-500 dark:text-zinc-400">{sub}</div>}
    </Comp>
  );
}

function StatsRow({ stats, loading, filter, setFilter }: { stats?: Stats; loading: boolean; filter: Filter; setFilter: (f: Filter) => void }) {
  const subParts = [];
  if (stats?.paused) subParts.push(`${stats.paused} paused`);
  if (stats?.error) subParts.push(`${stats.error} with errors`);
  return (
    <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
      <StatCard
        label="Tracking"
        value={stats?.total}
        loading={loading}
        icon={<Eye />}
        tone="bg-indigo-50 text-indigo-600 dark:bg-indigo-500/10 dark:text-indigo-400"
        sub={subParts.length ? subParts.join(' · ') : stats ? `${stats.checks_24h.toLocaleString()} checks in 24h` : undefined}
        onClick={() => setFilter('all')}
        active={filter === 'all'}
      />
      <StatCard
        label="In stock"
        value={stats?.in_stock}
        loading={loading}
        icon={<CheckCircle2 />}
        tone="bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400"
        sub={stats?.in_stock ? 'Go get it!' : 'Nothing yet'}
        onClick={() => setFilter('in_stock')}
        active={filter === 'in_stock'}
      />
      <StatCard
        label="Out of stock"
        value={stats?.out_of_stock}
        loading={loading}
        icon={<CircleSlash />}
        tone="bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-400"
        sub={stats ? `${stats.unknown} unknown` : undefined}
        onClick={() => setFilter('out_of_stock')}
        active={filter === 'out_of_stock'}
      />
      <Link
        to="/notifications"
        className="rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60"
      >
        <StatCard
          label="Alerts (24h)"
          value={stats?.alerts_24h}
          loading={loading}
          icon={<BellRing />}
          tone="bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400"
          sub={
            stats ? (
              stats.unread_notifications ? (
                <span className="font-medium text-indigo-600 dark:text-indigo-400">{stats.unread_notifications} unread</span>
              ) : (
                'All caught up'
              )
            ) : undefined
          }
        />
      </Link>
    </div>
  );
}

export default function Dashboard() {
  const items = useQuery({ queryKey: qk.items, queryFn: api.items, refetchInterval: 15_000 });
  const stats = useQuery({ queryKey: qk.stats, queryFn: api.stats, refetchInterval: 15_000 });
  const [params, setParams] = useSearchParams();
  const filter = (params.get('filter') as Filter) || 'all';
  const setFilter = (f: Filter) => {
    const p = new URLSearchParams(params);
    if (f === 'all') p.delete('filter');
    else p.set('filter', f);
    setParams(p, { replace: true });
  };
  const [search, setSearch] = useState('');
  const [sort, setSort] = useLocalStorage<Sort>('sw-sort', 'status');
  const [view, setView] = useLocalStorage<'grid' | 'list'>('sw-view', 'grid');

  const counts = useMemo(() => {
    const c: Record<Filter, number> = { all: 0, in_stock: 0, out_of_stock: 0, error: 0, paused: 0 };
    for (const it of items.data ?? []) for (const f of FILTERS) if (matches(it, f.id)) c[f.id]++;
    return c;
  }, [items.data]);

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = (items.data ?? []).filter(
      (it) =>
        matches(it, filter) &&
        (!q || it.name.toLowerCase().includes(q) || hostOf(it.url).includes(q) || (it.status_text ?? '').toLowerCase().includes(q)),
    );
    const byName = (a: Item, b: Item) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
    const ts = (s: string | null) => parseDate(s)?.getTime() ?? 0;
    return list.sort((a, b) => {
      if (sort === 'name') return byName(a, b);
      if (sort === 'change') return ts(b.last_change_at) - ts(a.last_change_at) || byName(a, b);
      const pa = a.enabled ? STATUS_ORDER[a.status] ?? 9 : 10;
      const pb = b.enabled ? STATUS_ORDER[b.status] ?? 9 : 10;
      return pa - pb || byName(a, b);
    });
  }, [items.data, filter, search, sort]);

  const hasItems = (items.data?.length ?? 0) > 0;

  return (
    <div className="space-y-6 sm:space-y-8">
      <PageHeader
        title="Dashboard"
        description="Everything you're watching, refreshed automatically."
        className="mb-0"
        actions={
          <Link to="/items/new" className={buttonClasses({ variant: 'primary', className: 'hidden sm:inline-flex' })}>
            <Plus /> Add item
          </Link>
        }
      />

      <StatsRow stats={stats.data} loading={stats.isLoading} filter={filter} setFilter={setFilter} />

      <section className="space-y-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="no-scrollbar -mx-4 flex gap-1.5 overflow-x-auto px-4 sm:mx-0 sm:px-0" role="tablist" aria-label="Filter items">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                role="tab"
                aria-selected={filter === f.id}
                onClick={() => setFilter(f.id)}
                className={cn(
                  'inline-flex h-8 shrink-0 items-center gap-2 rounded-full border px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60',
                  filter === f.id
                    ? 'border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900'
                    : 'border-zinc-200 bg-white text-zinc-600 hover:border-zinc-300 hover:text-zinc-900 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400 dark:hover:border-zinc-700 dark:hover:text-zinc-100',
                )}
              >
                {f.dot && <span className={cn('size-1.5 rounded-full', f.dot)} />}
                {f.label}
                <span
                  className={cn(
                    'tabular-nums text-xs',
                    filter === f.id ? 'text-white/70 dark:text-zinc-900/60' : 'text-zinc-400 dark:text-zinc-500',
                  )}
                >
                  {counts[f.id]}
                </span>
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1 lg:w-64 lg:flex-none">
              <Input
                type="search"
                placeholder="Search…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                leading={<Search />}
                trailing={
                  search ? (
                    <button type="button" onClick={() => setSearch('')} aria-label="Clear search" className="rounded p-0.5 hover:text-zinc-700 dark:hover:text-zinc-200">
                      <X />
                    </button>
                  ) : undefined
                }
                aria-label="Search items"
              />
            </div>
            <div className="relative w-[7.5rem] shrink-0 sm:w-36">
              <ArrowDownUp className="pointer-events-none absolute left-3 top-1/2 z-[1] size-3.5 -translate-y-1/2 text-zinc-400" />
              <Select value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label="Sort by" className="pl-8">
                <option value="status">Status</option>
                <option value="name">Name</option>
                <option value="change">Last change</option>
              </Select>
            </div>
            <div className="flex shrink-0 rounded-lg border border-zinc-200 bg-white p-0.5 shadow-soft dark:border-zinc-800 dark:bg-zinc-900/60" role="group" aria-label="View">
              {(['grid', 'list'] as const).map((v) => (
                <Tooltip key={v} content={v === 'grid' ? 'Grid view' : 'List view'}>
                  <button
                    type="button"
                    onClick={() => setView(v)}
                    aria-pressed={view === v}
                    aria-label={v === 'grid' ? 'Grid view' : 'List view'}
                    className={cn(
                      'flex size-7 items-center justify-center rounded-md transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60',
                      view === v
                        ? 'bg-zinc-100 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-50'
                        : 'text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200',
                    )}
                  >
                    {v === 'grid' ? <LayoutGrid className="size-4" /> : <List className="size-4" />}
                  </button>
                </Tooltip>
              ))}
            </div>
          </div>
        </div>

        {items.isError ? (
          <EmptyState
            icon={<AlertTriangle />}
            title="Couldn't load items"
            description={errorMessage(items.error)}
            action={<Button onClick={() => items.refetch()}>Retry</Button>}
          />
        ) : items.isLoading ? (
          view === 'grid' ? (
            <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3 xl:grid-cols-4">
              {Array.from({ length: 8 }).map((_, i) => (
                <ItemCardSkeleton key={i} />
              ))}
            </div>
          ) : (
            <Card className="divide-y divide-zinc-100 dark:divide-zinc-800/80">
              {Array.from({ length: 5 }).map((_, i) => (
                <ItemRowSkeleton key={i} />
              ))}
            </Card>
          )
        ) : !hasItems ? (
          <EmptyState
            icon={<PackageSearch />}
            title="Nothing on your watchlist yet"
            description="Paste a product link and Stock Watcher will check it every few minutes and ping you the moment it's back in stock."
            action={
              <Link to="/items/new" className={buttonClasses({ variant: 'primary' })}>
                <Plus /> Add your first item
              </Link>
            }
          />
        ) : !visible.length ? (
          <EmptyState
            icon={<Search />}
            title="No matching items"
            description={search ? `Nothing matches "${search}".` : 'No items in this filter right now.'}
            action={
              <Button
                onClick={() => {
                  setSearch('');
                  setFilter('all');
                }}
              >
                Clear filters
              </Button>
            }
          />
        ) : view === 'grid' ? (
          <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3 xl:grid-cols-4">
            {visible.map((it) => (
              <ItemCard key={it.id} item={it} />
            ))}
          </div>
        ) : (
          <Card className="divide-y divide-zinc-100 overflow-hidden dark:divide-zinc-800/80">
            {visible.map((it) => (
              <ItemRow key={it.id} item={it} />
            ))}
          </Card>
        )}
      </section>
    </div>
  );
}
