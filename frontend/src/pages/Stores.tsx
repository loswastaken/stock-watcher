import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, BadgeCheck, BellOff, ScanSearch, Search, Store } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { PageHeader } from '@/components/PageHeader';
import { PageLoader } from '@/components/PageLoader';
import { RetailerMonogram } from '@/components/RetailerBadge';
import { Badge } from '@/components/ui/badge';
import { Button, buttonClasses } from '@/components/ui/button';
import { EmptyState } from '@/components/ui/empty-state';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Tooltip } from '@/components/ui/tooltip';
import { api, errorMessage } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import { useRetailers } from '@/lib/retailers';
import type { Item, Retailer, Settings } from '@/lib/types';
import { cn } from '@/lib/utils';

/** "Tracked stores": every supported retailer, with a per-store alert mute. */
export default function StoresPage() {
  const retailers = useRetailers();
  const settings = useQuery({ queryKey: qk.settings, queryFn: api.settings, staleTime: 60_000 });
  const items = useQuery({ queryKey: qk.items, queryFn: api.items });
  const [query, setQuery] = useState('');
  const qc = useQueryClient();

  const muted = useMemo(() => new Set(settings.data?.muted_retailers ?? []), [settings.data]);
  const counts = useMemo(() => {
    const c = new Map<string, number>();
    for (const it of (items.data ?? []) as Item[]) {
      if (it.retailer && !it.purchased_at) c.set(it.retailer.key, (c.get(it.retailer.key) ?? 0) + 1);
    }
    return c;
  }, [items.data]);

  const setMuted = useMutation({
    mutationFn: (next: string[]) => api.updateSettings({ muted_retailers: next }),
    onMutate: async (next) => {
      await qc.cancelQueries({ queryKey: qk.settings });
      const prev = qc.getQueryData<Settings>(qk.settings);
      qc.setQueryData<Settings>(qk.settings, (s) => (s ? { ...s, muted_retailers: next } : s));
      return { prev };
    },
    onSuccess: (s) => qc.setQueryData(qk.settings, s),
    onError: (e, _next, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.settings, ctx.prev);
      toast.error("Couldn't update alerts", { description: errorMessage(e) });
    },
  });

  // Mute switches write the whole muted list, so they need the real settings first —
  // toggling against a missing list would wipe the other mutes.
  const settingsReady = !!settings.data;

  const toggle = (r: Retailer, alertsOn: boolean) => {
    if (!settings.data) return;
    const next = new Set(muted);
    if (alertsOn) next.delete(r.key);
    else next.add(r.key);
    setMuted.mutate([...next]);
  };

  if (retailers.isLoading || settings.isLoading) return <PageLoader />;
  if (retailers.isError)
    return (
      <EmptyState
        icon={<AlertTriangle />}
        title="Couldn't load stores"
        description={errorMessage(retailers.error)}
        action={<Button onClick={() => retailers.refetch()}>Try again</Button>}
      />
    );

  const settingsError = settings.isError && !settings.data;
  const q = query.trim().toLowerCase();
  const list = (retailers.data ?? []).filter((r) => !q || r.name.toLowerCase().includes(q) || r.domain.includes(q));
  const mutedCount = muted.size;

  return (
    <div>
      <PageHeader
        title="Tracked stores"
        description={
          <>
            {retailers.data?.length ?? 0} stores with dedicated support. Any other shop works too through auto-detection.
            {mutedCount > 0 && (
              <>
                {' '}
                <span className="font-medium text-amber-700 dark:text-amber-400">
                  {mutedCount} muted
                </span>
                .
              </>
            )}
          </>
        }
        actions={
          <div className="flex w-full items-center gap-2 sm:w-auto">
            <Link
              to="/?check"
              className={buttonClasses({ variant: 'outline', className: 'order-2 sm:order-none' })}
              title="Check any product link, even from stores not listed here"
            >
              <ScanSearch /> Check a link
            </Link>
            <div className="min-w-0 flex-1 sm:w-64 sm:flex-none">
              <Input
                placeholder="Search stores"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                leading={<Search />}
                aria-label="Search stores"
              />
            </div>
          </div>
        }
      />

      {settingsError && (
        <div
          role="alert"
          className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
        >
          <AlertTriangle className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">
            Couldn't load your alert settings, so store muting is unavailable. {errorMessage(settings.error)}
          </span>
          <Button size="sm" variant="outline" onClick={() => settings.refetch()} disabled={settings.isFetching}>
            Try again
          </Button>
        </div>
      )}

      {list.length === 0 ? (
        <EmptyState icon={<Store />} title="No matching stores" description="Try another name or domain." />
      ) : (
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
          {list.map((r) => {
            const alertsOn = !muted.has(r.key);
            const n = counts.get(r.key) ?? 0;
            return (
              <li
                key={r.key}
                className={cn(
                  'flex items-center gap-3 rounded-xl border bg-white p-3 shadow-soft transition-colors dark:bg-zinc-900/40',
                  alertsOn ? 'border-zinc-200 dark:border-zinc-800/80' : 'border-dashed border-zinc-300 dark:border-zinc-700',
                )}
              >
                <Tooltip content={r.note}>
                  <span className={cn(!alertsOn && 'opacity-50 grayscale')}>
                    <RetailerMonogram retailer={r} />
                  </span>
                </Tooltip>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <p className="truncate text-sm font-semibold text-zinc-900 dark:text-zinc-100">{r.name}</p>
                    {n > 0 && (
                      <span className="shrink-0 rounded-full bg-indigo-100 px-1.5 text-[11px] font-semibold tabular-nums text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300">
                        {n}
                      </span>
                    )}
                  </div>
                  <p className="truncate text-xs text-zinc-500 dark:text-zinc-400">{r.domain}</p>
                  <div className="mt-1.5 flex flex-wrap gap-1 empty:hidden">
                    {r.pickup && (
                      <Badge tone="sky">
                        <Store /> Pickup
                      </Badge>
                    )}
                    {r.seller_filter && (
                      <Badge tone="green">
                        <BadgeCheck /> Official seller
                      </Badge>
                    )}
                    {!alertsOn && (
                      <Badge tone="amber">
                        <BellOff /> Muted
                      </Badge>
                    )}
                  </div>
                </div>
                {settingsReady && (
                  <Tooltip content={alertsOn ? `Mute alerts from ${r.name}` : `Unmute ${r.name}`}>
                    <span>
                      <Switch
                        checked={alertsOn}
                        onCheckedChange={(v) => toggle(r, v)}
                        aria-label={`Alerts from ${r.name}`}
                      />
                    </span>
                  </Tooltip>
                )}
              </li>
            );
          })}
        </ul>
      )}
      <p className="mt-6 text-xs text-zinc-500 dark:text-zinc-400">
        Muting a store stops alerts for every item there; they're still checked and shown on the dashboard.
      </p>
    </div>
  );
}
