import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  ArrowLeft,
  BadgeCheck,
  Bell,
  BellOff,
  CalendarClock,
  CheckCircle2,
  Clock,
  ExternalLink,
  History,
  ImageDown,
  Info,
  MapPin,
  Pause,
  Pencil,
  Play,
  RefreshCw,
  ShoppingBag,
  ShoppingCart,
  Store,
  Timer,
  Truck,
  Undo2,
  XCircle,
  Zap,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { AppleBadge, RearmAlertsButton } from '@/components/items/ItemCard';
import { ItemActionsMenu } from '@/components/items/ItemActionsMenu';
import { RestockPanel, StoresPanel } from '@/components/items/ProductStores';
import { RetailerChip } from '@/components/RetailerBadge';
import { ItemImage } from '@/components/ItemImage';
import { RelativeTime } from '@/components/RelativeTime';
import { StatusBadge, StatusDot, statusMeta } from '@/components/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button, buttonClasses } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { Tooltip } from '@/components/ui/tooltip';
import { useIsChecking, useItemActions } from '@/hooks/useItemActions';
import { api, ApiError, errorMessage } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import type { AppleDelivery, AppleStore, CheckEvent, Item } from '@/lib/types';
import { fulfillmentLabel } from '@/lib/retailers';
import {
  absoluteTime,
  cn,
  formatDuration,
  formatInterval,
  formatMoney,
  hostOf,
  isAboveLimit,
  parseDate,
  relativeTime,
} from '@/lib/utils';

function isFresh(item: Item | undefined) {
  if (!item) return false;
  const created = parseDate(item.created_at)?.getTime() ?? 0;
  const young = Date.now() - created < 30_000;
  return young && (!item.last_checked_at || !item.image_url);
}

export default function ItemDetail() {
  const { id } = useParams();
  const itemId = Number(id);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const item = useQuery({
    queryKey: qk.item(itemId),
    queryFn: () => api.item(itemId),
    enabled: Number.isFinite(itemId),
    // New items get their photo + first check in the background: poll quickly for a bit.
    refetchInterval: (q) => (isFresh(q.state.data) ? 2_000 : 15_000),
  });
  const history = useQuery({
    queryKey: qk.history(itemId),
    queryFn: () => api.history(itemId, 50),
    enabled: Number.isFinite(itemId) && !!item.data,
  });

  // Refresh history whenever a new check lands.
  const lastChecked = item.data?.last_checked_at;
  const prevChecked = useRef(lastChecked);
  useEffect(() => {
    if (prevChecked.current !== lastChecked) {
      prevChecked.current = lastChecked;
      qc.invalidateQueries({ queryKey: qk.history(itemId) });
      qc.invalidateQueries({ queryKey: qk.restocks(itemId) });
      qc.invalidateQueries({ queryKey: qk.stores(itemId) });
    }
  }, [lastChecked, itemId, qc]);

  if (item.isLoading) return <DetailSkeleton />;
  if (item.isError || !item.data)
    return (
      <EmptyState
        icon={<AlertTriangle />}
        title={item.error instanceof ApiError && item.error.status === 404 ? 'Item not found' : "Couldn't load item"}
        description={item.error ? errorMessage(item.error) : undefined}
        action={
          <Link to="/" className={buttonClasses()}>
            Back to dashboard
          </Link>
        }
      />
    );

  const it = item.data;
  return (
    <div className="space-y-6">
      <Link
        to="/"
        className="inline-flex items-center gap-1.5 text-sm text-zinc-500 transition-colors hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
      >
        <ArrowLeft className="size-4" /> Dashboard
      </Link>
      <Hero item={it} onDeleted={() => navigate('/', { replace: true })} />

      {it.status === 'error' && it.last_error && (
        <div className="flex gap-3 rounded-xl border border-amber-300/60 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-500/20 dark:bg-amber-500/[0.07] dark:text-amber-200">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-500" />
          <div className="min-w-0">
            <p className="font-medium">Last check failed</p>
            <p className="mt-0.5 break-words text-amber-800/80 dark:text-amber-200/70">{it.last_error}</p>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="min-w-0 space-y-6">
          {it.kind === 'apple' ? <AppleResults item={it} /> : <WhyPanel item={it} />}
          {!it.purchased_at && <StoresPanel item={it} />}
          <RestockPanel item={it} />
        </div>
        <HistoryPanel events={history.data} loading={history.isLoading} error={history.error} />
      </div>
    </div>
  );
}

function Meta({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="flex items-center gap-1.5 text-xs text-zinc-500 dark:text-zinc-400 [&_svg]:size-3.5">
        {icon}
        {label}
      </dt>
      <dd className="mt-1 truncate text-sm font-medium text-zinc-900 dark:text-zinc-100">{children}</dd>
    </div>
  );
}

function Hero({ item, onDeleted }: { item: Item; onDeleted: () => void }) {
  const a = useItemActions();
  const checking = useIsChecking(item.id);
  const paused = !item.enabled;
  const m = statusMeta[item.status] ?? statusMeta.unknown;
  const fresh = isFresh(item);

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-col md:flex-row">
        <div className="relative md:w-72 md:shrink-0 lg:w-80">
          <ItemImage
            src={item.image_url}
            alt={item.name}
            className={cn(
              'aspect-[4/3] w-full md:aspect-square md:h-full md:border-r md:border-zinc-100 dark:md:border-zinc-800',
              paused && 'opacity-60 grayscale',
            )}
            iconClassName="size-12"
          />
          {fresh && !item.image_url && (
            <span className="absolute bottom-3 left-1/2 inline-flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-white/90 px-2.5 py-1 text-xs text-zinc-600 shadow ring-1 ring-zinc-200 dark:bg-zinc-900/90 dark:text-zinc-300 dark:ring-zinc-700">
              <RefreshCw className="size-3 animate-spin" /> Fetching photo…
            </span>
          )}
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-5 p-5 sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={item.status} paused={paused} />
                {paused && <StatusBadge status={item.status} />}
                {item.kind === 'apple' && <AppleBadge className="shadow-none ring-zinc-200 dark:ring-zinc-700" />}
                {item.kind !== 'apple' && item.retailer && (
                  <RetailerChip retailer={item.retailer} className="shadow-none ring-zinc-200 dark:ring-zinc-700" />
                )}
                {item.retailer_config && item.retailer?.pickup && (
                  <Badge tone="sky">
                    {item.retailer_config.fulfillment === 'delivery' ? <Truck /> : <Store />}
                    {fulfillmentLabel[item.retailer_config.fulfillment]}
                    {item.retailer_config.fulfillment !== 'delivery' && item.retailer_config.zip
                      ? ` · ${item.retailer_config.zip} (${item.retailer_config.radius_miles} mi)`
                      : ''}
                  </Badge>
                )}
                <RearmAlertsButton item={item} />
              </div>
              <h1 className="text-xl font-semibold leading-tight tracking-tight text-zinc-900 sm:text-2xl dark:text-zinc-50">{item.name}</h1>
              <a
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex max-w-full items-center gap-1.5 text-sm text-zinc-500 transition-colors hover:text-indigo-600 dark:text-zinc-400 dark:hover:text-indigo-400"
              >
                <span className="truncate">{hostOf(item.url)}</span>
                <ExternalLink className="size-3.5 shrink-0" />
              </a>
            </div>
            <ItemActionsMenu item={item} extended onDeleted={onDeleted} className="shrink-0" />
          </div>

          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="min-w-0">
              <p className={cn('text-base font-medium', paused ? 'text-zinc-500' : m.text)}>
                {fresh && !item.last_checked_at ? (
                  <span className="inline-flex items-center gap-2 text-zinc-500">
                    <RefreshCw className="size-4 animate-spin" /> Running first check…
                  </span>
                ) : (
                  item.status_text || m.label
                )}
              </p>
              {item.seller && (
                <p className="mt-1 flex items-center gap-1.5 text-xs text-zinc-500 dark:text-zinc-400">
                  {item.third_party ? (
                    <Badge tone="amber">Third-party seller</Badge>
                  ) : (
                    <BadgeCheck className="size-3.5 text-emerald-500" />
                  )}
                  Sold by {item.seller}
                </p>
              )}
            </div>
            {(item.price || item.max_price != null) && (
              <div className="text-right">
                {item.price && (
                  <p
                    className={cn(
                      'text-2xl font-semibold tabular-nums tracking-tight',
                      isAboveLimit(item) ? 'text-amber-700 dark:text-amber-400' : 'text-zinc-900 dark:text-zinc-50',
                    )}
                  >
                    {item.price}
                  </p>
                )}
                {item.max_price != null && (
                  <p className="text-xs tabular-nums text-zinc-500 dark:text-zinc-400">
                    {isAboveLimit(item) ? 'Above your ' : 'Price limit '}
                    {formatMoney(item.max_price)}
                    {isAboveLimit(item) ? ' limit' : ''}
                  </p>
                )}
              </div>
            )}
          </div>

          <dl className="grid grid-cols-2 gap-4 rounded-xl bg-zinc-50 p-4 sm:grid-cols-3 2xl:grid-cols-5 dark:bg-zinc-800/30">
            <Meta icon={<Timer />} label="Interval">
              Every {formatInterval(item.interval_minutes)}
            </Meta>
            <Meta icon={<Clock />} label="Last checked">
              <RelativeTime iso={item.last_checked_at} fallback="Not yet" />
            </Meta>
            <Meta icon={<History />} label="Last change">
              <RelativeTime iso={item.last_change_at} fallback="—" />
            </Meta>
            <Meta icon={<Zap />} label="Last in stock">
              <RelativeTime iso={item.last_in_stock_at} fallback="—" />
            </Meta>
            <Meta icon={item.notify_enabled ? <Bell /> : <BellOff />} label="Alerts">
              <span className="flex items-center gap-2">
                <Switch
                  checked={item.notify_enabled}
                  onCheckedChange={() => a.toggleNotify.mutate(item)}
                  aria-label="Alerts"
                  className="scale-90"
                />
                {item.notify_enabled ? 'On' : 'Paused'}
              </span>
            </Meta>
          </dl>

          {item.purchased_at && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-emerald-50 px-4 py-3 text-sm text-emerald-800 ring-1 ring-emerald-300/60 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/30">
              <span className="flex items-center gap-2">
                <ShoppingBag className="size-4" /> Purchased {relativeTime(item.purchased_at)}
                {item.purchased_price ? ` for ${item.purchased_price}` : ''}. No longer checked.
              </span>
              <Button size="sm" onClick={() => a.unpurchase.mutate(item)} loading={a.unpurchase.isPending}>
                <Undo2 /> Watch again
              </Button>
            </div>
          )}

          <div className="mt-auto flex flex-wrap gap-2">
            {item.cart_url && !item.purchased_at && (
              <a
                href={item.cart_url}
                target="_blank"
                rel="noopener noreferrer"
                className={buttonClasses({ variant: item.status === 'in_stock' ? 'primary' : 'outline' })}
              >
                <ShoppingCart /> Add to cart
              </a>
            )}
            {!item.purchased_at && (
              <Button
                variant={item.cart_url && item.status === 'in_stock' ? 'outline' : 'primary'}
                onClick={() => a.purchase.mutate(item)}
                loading={a.purchase.isPending}
              >
                <ShoppingBag /> Mark purchased
              </Button>
            )}
            <Button variant={item.purchased_at ? 'primary' : 'outline'} loading={checking} onClick={() => a.check.mutate(item)}>
              {!checking && <RefreshCw />} {checking ? 'Checking…' : 'Check now'}
            </Button>
            <Link to={`/items/${item.id}/edit`} className={buttonClasses()}>
              <Pencil /> Edit
            </Link>
            <Button onClick={() => a.toggle.mutate(item)}>
              {item.enabled ? (
                <>
                  <Pause /> Pause
                </>
              ) : (
                <>
                  <Play /> Resume
                </>
              )}
            </Button>
            <Button variant="ghost" loading={a.refreshImage.isPending} onClick={() => a.refreshImage.mutate(item)}>
              {!a.refreshImage.isPending && <ImageDown />} Refresh photo
            </Button>
          </div>
        </div>
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------------- Apple */

function AvailabilityPill({ available, today, className }: { available: boolean; today?: boolean; className?: string }) {
  const base = 'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px]';
  // `today` missing (older results) => treat available as today.
  if (available && today !== false)
    return (
      <span className={cn(base, 'bg-emerald-100 font-semibold text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-400', className)}>
        <CheckCircle2 className="size-3" /> Today
      </span>
    );
  if (available)
    return (
      <span className={cn(base, 'bg-amber-100 font-semibold text-amber-800 dark:bg-amber-500/15 dark:text-amber-400', className)}>
        <CalendarClock className="size-3" /> Later
      </span>
    );
  return (
    <span className={cn(base, 'bg-zinc-100 font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400', className)}>
      <XCircle className="size-3" /> Unavailable
    </span>
  );
}

function AppleResults({ item }: { item: Item }) {
  const stores = useMemo(() => {
    const list = (item.last_result?.stores ?? []) as AppleStore[];
    return [...list].sort((a, b) => (a.distance_miles ?? Infinity) - (b.distance_miles ?? Infinity));
  }, [item.last_result]);
  const delivery = (item.last_result?.delivery ?? []) as AppleDelivery[];
  const [onlyAvail, setOnlyAvail] = useState(false);
  const cfg = item.apple_config;
  const availCount = stores.filter((s) => s.parts.some((p) => p.available)).length;
  const shown = onlyAvail ? stores.filter((s) => s.parts.some((p) => p.available)) : stores;
  const multiPart = (cfg?.parts.length ?? 0) > 1 || stores.some((s) => s.parts.length > 1);
  const outOfRange = Number(item.last_result?.stores_out_of_range ?? 0);
  const pickupMessage = item.last_result?.pickup_message as string | null | undefined;
  const zip = (item.last_result?.zip as string | undefined) || cfg?.zip;

  return (
    <>
      {cfg?.watch_delivery !== false && (
        <Card>
          <CardHeader icon={<Truck />} title="2-hour delivery" description={zip ? `Courier delivery to ${zip}` : undefined} />
          <CardBody className="p-0">
            {delivery.length ? (
              <ul className="divide-y divide-zinc-100 dark:divide-zinc-800/80">
                {delivery.map((d) => (
                  <li
                    key={d.part_number}
                    className={cn(
                      'flex items-center justify-between gap-3 px-5 py-3',
                      d.two_hour && 'bg-emerald-50/60 dark:bg-emerald-500/[0.06]',
                    )}
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-zinc-900 dark:text-zinc-100">{d.label || d.part_number}</p>
                      <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">
                        <span className="font-mono">{d.part_number}</span>
                        {d.quote && <> · {d.quote}</>}
                      </p>
                    </div>
                    {d.two_hour ? (
                      <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-emerald-100 px-2.5 py-1 text-xs font-semibold text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-400">
                        <CheckCircle2 className="size-3.5" /> 2-hour
                      </span>
                    ) : (
                      <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-zinc-100 px-2.5 py-1 text-xs font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                        <XCircle className="size-3.5" /> Not available
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="px-5 py-6 text-sm text-zinc-500 dark:text-zinc-400">
                {item.last_checked_at ? 'No delivery information from the last check.' : 'Waiting for the first check…'}
              </p>
            )}
          </CardBody>
        </Card>
      )}

      {cfg?.watch_pickup !== false && (
        <Card>
          <CardHeader
            icon={<Store />}
            title="In-store pickup"
            description={
              stores.length
                ? `Available at ${availCount} of ${stores.length} store${stores.length === 1 ? '' : 's'}${cfg ? ` within ${cfg.max_distance_miles} mi of ${zip}` : ''}`
                : cfg
                  ? `Stores within ${cfg.max_distance_miles} mi of ${zip}`
                  : undefined
            }
            actions={
              stores.length > 0 && (
                <label className="flex cursor-pointer items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
                  <span className="hidden sm:inline">Available only</span>
                  <Switch checked={onlyAvail} onCheckedChange={setOnlyAvail} aria-label="Show available stores only" />
                </label>
              )
            }
          />
          {stores.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-100 text-left text-xs text-zinc-500 dark:border-zinc-800/80 dark:text-zinc-400">
                    <th className="px-5 py-2.5 font-medium">Store</th>
                    <th className="hidden px-3 py-2.5 text-right font-medium sm:table-cell">Distance</th>
                    <th className="px-5 py-2.5 font-medium">Availability</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800/80">
                  {shown.map((s) => {
                    const any = s.parts.some((p) => p.available);
                    const anyToday = s.parts.some((p) => p.available && p.today !== false);
                    return (
                      <tr
                        key={s.store_number}
                        className={cn(
                          'align-top',
                          anyToday ? 'bg-emerald-50/60 dark:bg-emerald-500/[0.06]' : any && 'bg-amber-50/50 dark:bg-amber-500/[0.04]',
                        )}
                      >
                        <td className="px-5 py-3">
                          <div className="flex items-start gap-2">
                            <span
                              className={cn(
                                'mt-1.5 size-2 shrink-0 rounded-full',
                                anyToday ? 'bg-emerald-500' : any ? 'bg-amber-500' : 'bg-zinc-300 dark:bg-zinc-700',
                              )}
                            />
                            <div className="min-w-0">
                              <p className="font-medium text-zinc-900 dark:text-zinc-100">{s.name}</p>
                              <p className="text-xs text-zinc-500 dark:text-zinc-400">
                                {s.city}
                                {s.distance_miles != null && <span className="sm:hidden"> · {s.distance_miles.toFixed(1)} mi</span>}
                              </p>
                            </div>
                          </div>
                        </td>
                        <td className="hidden whitespace-nowrap px-3 py-3 text-right tabular-nums text-zinc-600 sm:table-cell dark:text-zinc-400">
                          {s.distance_miles != null ? `${s.distance_miles.toFixed(1)} mi` : '—'}
                        </td>
                        <td className="px-5 py-3">
                          <ul className="space-y-1.5">
                            {s.parts.map((p) => (
                              <li key={p.part_number} className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                                <AvailabilityPill available={p.available} today={p.today} />
                                {multiPart && (
                                  <span className="text-xs font-medium text-zinc-700 dark:text-zinc-300">{p.label || p.part_number}</span>
                                )}
                                {p.quote && (
                                  <span className={cn('text-xs', p.available && p.today === false ? 'font-medium text-amber-700 dark:text-amber-400' : 'text-zinc-500 dark:text-zinc-400')}>
                                    {p.quote}
                                  </span>
                                )}
                              </li>
                            ))}
                          </ul>
                        </td>
                      </tr>
                    );
                  })}
                  {!shown.length && (
                    <tr>
                      <td colSpan={3} className="px-5 py-6 text-center text-sm text-zinc-500 dark:text-zinc-400">
                        No stores have stock right now.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
              {outOfRange > 0 && (
                <p className="border-t border-zinc-100 px-5 py-2.5 text-xs text-zinc-500 dark:border-zinc-800/80 dark:text-zinc-400">
                  {outOfRange} store{outOfRange === 1 ? '' : 's'} beyond {cfg?.max_distance_miles ?? '?'} mi hidden
                </p>
              )}
            </div>
          ) : (
            <CardBody>
              <p className="flex items-center gap-2 text-sm text-zinc-500 dark:text-zinc-400">
                <MapPin className="size-4" />
                {pickupMessage ||
                  (item.last_checked_at ? 'No stores returned by the last check.' : 'Waiting for the first check…')}
              </p>
              {outOfRange > 0 && (
                <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
                  {outOfRange} store{outOfRange === 1 ? '' : 's'} beyond {cfg?.max_distance_miles ?? '?'} mi hidden
                </p>
              )}
            </CardBody>
          )}
        </Card>
      )}

      {cfg && cfg.parts.length > 0 && (
        <Card>
          <CardHeader title="Watched models" icon={<Info />} />
          <CardBody className="flex flex-wrap gap-2">
            {cfg.parts.map((p) => (
              <span
                key={p.part_number}
                className="inline-flex items-center gap-2 rounded-lg border border-zinc-200 px-2.5 py-1.5 text-xs dark:border-zinc-800"
              >
                <span className="font-medium text-zinc-800 dark:text-zinc-200">{p.label || p.part_number}</span>
                <span className="font-mono text-zinc-500">{p.part_number}</span>
              </span>
            ))}
          </CardBody>
        </Card>
      )}
    </>
  );
}

/* -------------------------------------------------------------- Generic */

function WhyPanel({ item }: { item: Item }) {
  const signals = (item.last_result?.signals ?? []) as string[];
  const matched = item.last_result?.matched as string | null | undefined;
  const cfg = item.generic_config;
  const modeLabel = cfg?.mode === 'selector' ? 'CSS selector' : cfg?.mode === 'text' ? 'Text match' : 'Auto-detect';
  return (
    <Card>
      <CardHeader
        icon={<Info />}
        title="Why this status?"
        description="Signals found on the page during the last check."
        actions={
          <div className="flex items-center gap-1.5">
            <Badge tone="indigo">{modeLabel}</Badge>
            {cfg?.render_js && <Badge tone="sky">JS</Badge>}
          </div>
        }
      />
      <CardBody className="space-y-4">
        {cfg?.mode === 'selector' && cfg.selector && (
          <div className="rounded-lg bg-zinc-50 px-3 py-2 font-mono text-xs text-zinc-700 dark:bg-zinc-800/40 dark:text-zinc-300">
            {cfg.selector}
          </div>
        )}
        {signals.length ? (
          <ul className="space-y-2">
            {signals.map((sig, i) => {
              const [src, ...rest] = sig.split(':');
              const hasSrc = rest.length > 0 && src.length < 24;
              return (
                <li key={i} className="flex items-start gap-2.5 text-sm">
                  <StatusDot status={item.status} className="mt-1.5" />
                  <span className="min-w-0 break-words text-zinc-700 dark:text-zinc-300">
                    {hasSrc ? (
                      <>
                        <span className="mr-1.5 rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
                          {src.trim()}
                        </span>
                        {rest.join(':').trim()}
                      </>
                    ) : (
                      sig
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            {item.last_checked_at ? 'No detailed signals were recorded for the last check.' : 'Waiting for the first check…'}
          </p>
        )}
        {matched && (
          <div className="border-t border-zinc-100 pt-3 dark:border-zinc-800">
            <p className="text-xs font-medium text-zinc-500 dark:text-zinc-400">Matched</p>
            <p className="mt-1 break-words rounded-lg bg-zinc-50 px-3 py-2 text-sm text-zinc-700 dark:bg-zinc-800/40 dark:text-zinc-300">
              “{matched}”
            </p>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/* -------------------------------------------------------------- History */

function AvailabilityStrip({ events }: { events: CheckEvent[] }) {
  const ordered = [...events].reverse();
  const inStock = events.filter((e) => e.status === 'in_stock').length;
  const pct = events.length ? Math.round((inStock / events.length) * 100) : 0;
  return (
    <div>
      <div className="mb-2 flex items-center justify-between text-xs text-zinc-500 dark:text-zinc-400">
        <span>Last {events.length} checks</span>
        <span className="tabular-nums">{pct}% in stock</span>
      </div>
      <div className="flex h-8 items-stretch gap-[2px]" role="img" aria-label={`Availability over the last ${events.length} checks: ${pct}% in stock`}>
        {ordered.map((e) => (
          <Tooltip
            key={e.id}
            content={
              <span>
                {statusMeta[e.status]?.label ?? e.status} · {absoluteTime(e.checked_at)}
              </span>
            }
          >
            <span
              className={cn(
                'min-w-[3px] flex-1 rounded-sm opacity-90 transition-opacity hover:opacity-100',
                statusMeta[e.status]?.bar ?? 'bg-zinc-400',
                e.status === 'in_stock' ? 'h-full' : e.status === 'out_of_stock' ? 'mt-auto h-1/2' : 'mt-auto h-3/4',
              )}
            />
          </Tooltip>
        ))}
      </div>
      <div className="mt-1.5 flex justify-between text-[11px] text-zinc-400 dark:text-zinc-500">
        <span>older</span>
        <span>now</span>
      </div>
    </div>
  );
}

function HistoryPanel({ events, loading, error }: { events?: CheckEvent[]; loading: boolean; error: unknown }) {
  return (
    <Card className="self-start">
      <CardHeader icon={<History />} title="Check history" />
      <CardBody className="space-y-5">
        {loading ? (
          <div className="space-y-3">
            <Skeleton className="h-8 w-full" />
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : error ? (
          <p className="text-sm text-rose-600 dark:text-rose-400">{errorMessage(error)}</p>
        ) : !events?.length ? (
          <p className="text-sm text-zinc-500 dark:text-zinc-400">No checks yet.</p>
        ) : (
          <>
            <AvailabilityStrip events={events} />
            <ol className="relative max-h-[32rem] space-y-0 overflow-y-auto pr-1">
              {events.map((e, i) => (
                <li key={e.id} className="relative flex gap-3 pb-4 last:pb-0">
                  {i < events.length - 1 && (
                    <span aria-hidden className="absolute left-[5px] top-4 h-[calc(100%-8px)] w-px bg-zinc-200 dark:bg-zinc-800" />
                  )}
                  <span
                    className={cn(
                      'relative mt-1 size-[11px] shrink-0 rounded-full ring-4 ring-white dark:ring-zinc-900',
                      statusMeta[e.status]?.dot ?? 'bg-zinc-400',
                      !e.changed && 'opacity-60',
                    )}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <p className="truncate text-sm">
                        <span className={cn('font-medium', statusMeta[e.status]?.text)}>{statusMeta[e.status]?.label ?? e.status}</span>
                        {e.changed && (
                          <span className="ml-2 rounded bg-indigo-100 px-1 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300">
                            changed
                          </span>
                        )}
                      </p>
                      <RelativeTime iso={e.checked_at} className="shrink-0 text-xs text-zinc-400 dark:text-zinc-500" />
                    </div>
                    {(e.status_text || e.error) && (
                      <p className={cn('mt-0.5 break-words text-xs', e.error ? 'text-amber-700 dark:text-amber-400/90' : 'text-zinc-500 dark:text-zinc-400')}>
                        {e.error || e.status_text}
                      </p>
                    )}
                    {e.duration_ms != null && (
                      <p className="mt-0.5 text-[11px] text-zinc-400 dark:text-zinc-500">took {formatDuration(e.duration_ms)}</p>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          </>
        )}
      </CardBody>
    </Card>
  );
}

function DetailSkeleton() {
  return (
    <div className="space-y-6">
      <Skeleton className="h-5 w-24" />
      <Card className="flex flex-col overflow-hidden md:flex-row">
        <Skeleton className="aspect-[4/3] w-full rounded-none md:aspect-square md:w-80" />
        <div className="flex-1 space-y-4 p-6">
          <Skeleton className="h-5 w-24 rounded-full" />
          <Skeleton className="h-7 w-2/3" />
          <Skeleton className="h-4 w-40" />
          <Skeleton className="h-20 w-full rounded-xl" />
          <div className="flex gap-2">
            <Skeleton className="h-9 w-28" />
            <Skeleton className="h-9 w-20" />
          </div>
        </div>
      </Card>
    </div>
  );
}
