import { useQueryClient } from '@tanstack/react-query';
import {
  AlertOctagon,
  BadgeCheck,
  Blocks,
  ChevronDown,
  Clock,
  ExternalLink,
  Eye,
  Globe,
  History,
  Info,
  Loader2,
  RefreshCw,
  ScanSearch,
  ShieldAlert,
  ShoppingCart,
  Sparkles,
  Store,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ItemImage } from '@/components/ItemImage';
import { RetailerChip } from '@/components/RetailerBadge';
import { StatusBadge, StatusDot, statusMeta } from '@/components/StatusBadge';
import { Badge } from '@/components/ui/badge';
import { Button, buttonClasses } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { useNow } from '@/hooks/useNow';
import { api, errorMessage } from '@/lib/api';
import type { ItemStatus, Preview, SupportLevel } from '@/lib/types';
import { cn, hostOf, isValidUrl, relativeTime } from '@/lib/utils';

/** Shared with ItemForm's autofill query, so "Watch this" doesn't fetch the page again. */
export const previewKey = (url: string) => ['preview', url] as const;

const HISTORY_KEY = 'sw-quickcheck-history';
const HISTORY_MAX = 5;
const SLOW_AFTER_S = 5;

interface Checked {
  url: string;
  data: Preview;
  checkedAt: number;
}

/** Pull a URL out of whatever was typed/pasted; add https:// to a bare "shop.com/…". */
export function normalizeUrl(raw: string): string {
  const t = raw.trim();
  const m = t.match(/https?:\/\/[^\s<>"']+/i);
  if (m) return m[0];
  if (/^[\w-]+(\.[\w-]+)+(\/\S*)?$/.test(t)) return `https://${t}`;
  return t;
}

const supportMeta: Record<SupportLevel, { box: string; icon: string; Icon: typeof BadgeCheck; short: string }> = {
  dedicated: {
    box: 'border-emerald-200 bg-emerald-50/70 dark:border-emerald-500/25 dark:bg-emerald-500/10',
    icon: 'text-emerald-600 dark:text-emerald-400',
    Icon: BadgeCheck,
    short: 'Supported',
  },
  platform: {
    box: 'border-sky-200 bg-sky-50/70 dark:border-sky-500/25 dark:bg-sky-500/10',
    icon: 'text-sky-600 dark:text-sky-400',
    Icon: Blocks,
    short: 'Platform',
  },
  generic: {
    box: 'border-amber-200 bg-amber-50/70 dark:border-amber-500/25 dark:bg-amber-500/10',
    icon: 'text-amber-600 dark:text-amber-400',
    Icon: Sparkles,
    short: 'Generic',
  },
  blocked: {
    box: 'border-rose-200 bg-rose-50/70 dark:border-rose-500/25 dark:bg-rose-500/10',
    icon: 'text-rose-600 dark:text-rose-400',
    Icon: ShieldAlert,
    short: 'Blocked',
  },
  unsupported: {
    box: 'border-rose-200 bg-rose-50/70 dark:border-rose-500/25 dark:bg-rose-500/10',
    icon: 'text-rose-600 dark:text-rose-400',
    Icon: AlertOctagon,
    short: 'Unsupported',
  },
};

const VIA_LABEL: Record<string, string> = {
  browser: 'Fetched with a real browser',
  curl: 'Fetched with a browser-like HTTP client',
  http: 'Fetched with a plain HTTP request',
};

function asStatus(s: string | null | undefined): ItemStatus {
  return s && s in statusMeta ? (s as ItemStatus) : 'unknown';
}

function loadHistory(v: unknown): Checked[] {
  return Array.isArray(v) ? v.filter((h): h is Checked => !!h && typeof h.url === 'string' && !!h.data).slice(0, HISTORY_MAX) : [];
}

export function QuickCheck({ className }: { className?: string }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [collapsed, setCollapsed] = useLocalStorage('sw-quickcheck-collapsed', false);
  const [rawHistory, setHistory] = useLocalStorage<Checked[]>(HISTORY_KEY, []);
  const history = loadHistory(rawHistory);
  const [input, setInput] = useState('');
  const [invalid, setInvalid] = useState(false);
  const [shown, setShown] = useState<Checked | null>(null);
  const [pending, setPending] = useState<{ url: string; startedAt: number } | null>(null);
  const [failure, setFailure] = useState<{ url: string; message: string } | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [focusReq, setFocusReq] = useState(0);
  const runId = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const historyRef = useRef(history);
  historyRef.current = history;

  useEffect(() => {
    if (!pending) return;
    setElapsed(0);
    const t = setInterval(() => setElapsed(Math.floor((Date.now() - pending.startedAt) / 1000)), 1000);
    return () => clearInterval(t);
  }, [pending]);

  const remember = useCallback(
    (entry: Checked) => {
      // Keep history small: drop data: URI images (mock/inline photos) larger than a few KB.
      const data = { ...entry.data };
      if (data.image_url?.startsWith('data:') && data.image_url.length > 8_000) data.image_url = null;
      const next = [{ ...entry, data }, ...historyRef.current.filter((h) => h.url !== entry.url)].slice(0, HISTORY_MAX);
      setHistory(next);
    },
    [setHistory],
  );

  const check = useCallback(
    async (raw: string, force = false) => {
      const url = normalizeUrl(raw);
      if (!isValidUrl(url)) {
        setInvalid(true);
        inputRef.current?.focus();
        return;
      }
      setInput(url);
      setInvalid(false);
      setFailure(null);
      setCollapsed(false);
      const id = ++runId.current;
      setPending({ url, startedAt: Date.now() });
      try {
        const data = await qc.fetchQuery({
          queryKey: previewKey(url),
          queryFn: ({ signal }) => api.preview(url, signal),
          staleTime: force ? 0 : 60_000,
          retry: false,
        });
        if (id !== runId.current) return;
        const entry = { url, data, checkedAt: qc.getQueryState(previewKey(url))?.dataUpdatedAt || Date.now() };
        setShown(entry);
        remember(entry);
      } catch (e) {
        if (id !== runId.current) return;
        setFailure({ url, message: errorMessage(e) });
      } finally {
        if (id === runId.current) setPending(null);
      }
    },
    [qc, remember, setCollapsed],
  );

  const cancel = () => {
    if (!pending) return;
    runId.current++;
    void qc.cancelQueries({ queryKey: previewKey(pending.url), exact: true });
    setPending(null);
  };

  const showHistory = (h: Checked) => {
    runId.current++;
    setPending(null);
    setFailure(null);
    setInput(h.url);
    setInvalid(false);
    // Seed the shared cache so "Watch this" can reuse it (ItemForm refetches it once it's stale).
    if (!qc.getQueryData(previewKey(h.url))) qc.setQueryData(previewKey(h.url), h.data, { updatedAt: h.checkedAt });
    setShown(h);
  };

  // Deep links: "/?check" opens + focuses the card, "/?check=<url>" runs a check straight away.
  const deepLink = params.get('check');
  useEffect(() => {
    if (deepLink === null) return;
    const p = new URLSearchParams(params);
    p.delete('check');
    setParams(p, { replace: true });
    setCollapsed(false);
    setFocusReq((n) => n + 1);
    if (deepLink) void check(deepLink);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deepLink]);
  // Runs after the expanded body has rendered, so the input exists.
  useEffect(() => {
    if (!focusReq || collapsed) return;
    const t = setTimeout(() => {
      rootRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
      inputRef.current?.focus({ preventScroll: true });
    }, 50);
    return () => clearTimeout(t);
  }, [focusReq, collapsed]);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void check(input);
  };

  const watch = (url: string) => navigate(`/items/new?url=${encodeURIComponent(url)}`);

  return (
    <div ref={rootRef} id="quick-check" className={cn('scroll-mt-20', className)}>
      <Card className="overflow-hidden">
        <div className="flex items-center gap-3 px-4 py-3 sm:px-5">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600 dark:bg-indigo-500/10 dark:text-indigo-400 [&_svg]:size-4">
            <ScanSearch />
          </div>
          <button
            type="button"
            onClick={() => setCollapsed(!collapsed)}
            aria-expanded={!collapsed}
            aria-controls="quick-check-body"
            className="min-w-0 flex-1 text-left focus-visible:outline-none"
          >
            <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Quick check</h2>
            <p className="truncate text-xs text-zinc-500 sm:text-sm dark:text-zinc-400">
              {collapsed && pending
                ? `Checking ${hostOf(pending.url)}…`
                : 'Paste any product link to see if it’s in stock — even stores that aren’t on the list.'}
            </p>
          </button>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? 'Expand quick check' : 'Collapse quick check'}
            aria-expanded={!collapsed}
          >
            <ChevronDown className={cn('transition-transform', !collapsed && 'rotate-180')} />
          </Button>
        </div>

        {!collapsed && (
          <div id="quick-check-body" className="space-y-4 border-t border-zinc-100 px-4 pb-4 pt-4 sm:px-5 sm:pb-5 dark:border-zinc-800/80">
            <form onSubmit={onSubmit} className="flex flex-col gap-2 sm:flex-row" noValidate>
              <div className="min-w-0 flex-1">
                <Input
                  ref={inputRef}
                  type="url"
                  inputMode="url"
                  autoComplete="off"
                  enterKeyHint="go"
                  placeholder="https://store.example.com/product/…"
                  aria-label="Product link to check"
                  value={input}
                  invalid={invalid}
                  onChange={(e) => {
                    setInput(e.target.value);
                    setInvalid(false);
                  }}
                  onPaste={(e) => {
                    const t = e.clipboardData.getData('text');
                    const url = normalizeUrl(t);
                    if (isValidUrl(url)) {
                      e.preventDefault();
                      void check(url);
                    }
                  }}
                  leading={<Globe />}
                  trailing={
                    input && !pending ? (
                      <button
                        type="button"
                        onClick={() => {
                          setInput('');
                          setInvalid(false);
                          inputRef.current?.focus();
                        }}
                        aria-label="Clear link"
                        className="rounded p-0.5 hover:text-zinc-700 dark:hover:text-zinc-200"
                      >
                        <X />
                      </button>
                    ) : undefined
                  }
                  className="h-10"
                />
                {invalid && <p className="mt-1.5 text-xs text-rose-600 dark:text-rose-400">Enter a full product link (https://…).</p>}
              </div>
              {pending ? (
                <Button variant="outline" onClick={cancel} className="h-10 sm:w-28">
                  <X /> Cancel
                </Button>
              ) : (
                <Button type="submit" variant="primary" className="h-10 sm:w-28" disabled={!input.trim()}>
                  <ScanSearch /> Check
                </Button>
              )}
            </form>

            {pending ? (
              <PendingCard url={pending.url} elapsed={elapsed} />
            ) : failure ? (
              <div
                role="alert"
                className="flex flex-wrap items-center gap-3 rounded-xl border border-rose-200 bg-rose-50/70 p-3 text-sm text-rose-900 dark:border-rose-500/25 dark:bg-rose-500/10 dark:text-rose-200"
              >
                <AlertOctagon className="size-4 shrink-0" />
                <span className="min-w-0 flex-1">
                  Couldn’t check {hostOf(failure.url)}: {failure.message}
                </span>
                <Button size="sm" variant="outline" onClick={() => void check(failure.url, true)}>
                  Try again
                </Button>
              </div>
            ) : shown ? (
              <ResultCard
                key={`${shown.url}-${shown.checkedAt}`}
                entry={shown}
                onRecheck={() => void check(shown.url, true)}
                onWatch={() => watch(shown.url)}
                onDismiss={() => setShown(null)}
              />
            ) : null}

            {history.length > 0 && (
              <HistoryList
                history={history}
                activeUrl={pending?.url ?? shown?.url}
                onPick={showHistory}
                onClear={() => setHistory([])}
              />
            )}
          </div>
        )}
      </Card>
    </div>
  );
}

function PendingCard({ url, elapsed }: { url: string; elapsed: number }) {
  const slow = elapsed >= SLOW_AFTER_S;
  return (
    <div className="rounded-xl border border-zinc-200 p-3 sm:p-4 dark:border-zinc-800" aria-busy="true" aria-live="polite">
      <div className="flex gap-3 sm:gap-4">
        <Skeleton className="size-24 shrink-0 rounded-lg sm:size-32" />
        <div className="min-w-0 flex-1 space-y-2.5 py-1">
          <Skeleton className="h-3.5 w-24" />
          <Skeleton className="h-4 w-full max-w-md" />
          <Skeleton className="h-4 w-2/3 max-w-xs" />
          <div className="flex gap-2 pt-1">
            <Skeleton className="h-6 w-24 rounded-full" />
            <Skeleton className="h-6 w-16" />
          </div>
        </div>
      </div>
      <div className="mt-3 flex items-start gap-2 text-xs text-zinc-500 dark:text-zinc-400">
        <Loader2 className="mt-px size-3.5 shrink-0 animate-spin text-indigo-500" />
        <p className="min-w-0">
          <span className="font-medium text-zinc-700 dark:text-zinc-300">Checking {hostOf(url)}…</span>{' '}
          <span className="tabular-nums">{elapsed} s</span>
          {slow && (
            <span className="block sm:inline">
              <span className="hidden sm:inline"> · </span>
              Using a real browser — some stores take up to a minute.
            </span>
          )}
        </p>
      </div>
    </div>
  );
}

function StoreChip({ data, url }: { data: Preview; url: string }) {
  if (data.retailer) return <RetailerChip retailer={data.retailer} className="shadow-none ring-zinc-200 dark:ring-zinc-700" />;
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1">
      <span className="inline-flex min-w-0 max-w-full items-center gap-1 rounded-full bg-zinc-100 px-2 py-0.5 text-[11px] font-medium text-zinc-700 ring-1 ring-zinc-200 dark:bg-zinc-800 dark:text-zinc-200 dark:ring-zinc-700">
        <Globe className="size-3 shrink-0" />
        <span className="truncate">{hostOf(url)}</span>
      </span>
      <span className="shrink-0 text-[11px] text-zinc-500 dark:text-zinc-400">Not on the list</span>
    </span>
  );
}

function SupportBox({ data }: { data: Preview }) {
  const s = data.support;
  if (!s) return null;
  const m = supportMeta[s.level] ?? supportMeta.generic;
  return (
    <div className={cn('flex gap-2.5 rounded-lg border p-3', m.box)} data-support={s.level}>
      <m.Icon className={cn('mt-0.5 size-4 shrink-0', m.icon)} />
      <div className="min-w-0 text-sm">
        <p className="font-medium text-zinc-900 dark:text-zinc-100">{s.label}</p>
        {s.detail && <p className="mt-0.5 text-xs leading-relaxed text-zinc-600 dark:text-zinc-400">{s.detail}</p>}
      </div>
    </div>
  );
}

function adapterLabel(data: Preview): string | null {
  const a = data.adapter;
  if (!a) return null;
  if (a === 'generic') return 'Generic page analysis';
  if (data.retailer && data.retailer.key === a) return `${data.retailer.name} integration`;
  const platforms: Record<string, string> = {
    shopify: 'Shopify',
    sfcc: 'Salesforce Commerce Cloud',
    magento: 'Magento',
    bigcommerce: 'BigCommerce',
    woocommerce: 'WooCommerce',
    opencart: 'OpenCart',
  };
  return platforms[a] ? `${platforms[a]} recipe` : `${a} integration`;
}

function WhySection({ data }: { data: Preview }) {
  const [open, setOpen] = useState(false);
  const signals = data.signals ?? [];
  const via = data.fetched_via ? VIA_LABEL[data.fetched_via] ?? `Fetched via ${data.fetched_via}` : null;
  const adapter = adapterLabel(data);
  if (!signals.length && !via && !adapter && !data.status_text) return null;
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-xs font-medium text-zinc-600 hover:text-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 dark:text-zinc-400 dark:hover:text-zinc-100"
      >
        <span className="inline-flex items-center gap-1.5">
          <Info className="size-3.5" /> Why this result?
        </span>
        <ChevronDown className={cn('size-3.5 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="space-y-2 border-t border-zinc-100 px-3 py-2.5 text-xs text-zinc-600 dark:border-zinc-800 dark:text-zinc-400">
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
            {data.status_text && (
              <>
                <dt className="text-zinc-400 dark:text-zinc-500">Page says</dt>
                <dd className="break-words text-zinc-700 dark:text-zinc-300">{data.status_text}</dd>
              </>
            )}
            {adapter && (
              <>
                <dt className="text-zinc-400 dark:text-zinc-500">Checked by</dt>
                <dd className="text-zinc-700 dark:text-zinc-300">{adapter}</dd>
              </>
            )}
            {via && (
              <>
                <dt className="text-zinc-400 dark:text-zinc-500">Fetch</dt>
                <dd className="text-zinc-700 dark:text-zinc-300">{via}</dd>
              </>
            )}
          </dl>
          {signals.length > 0 && (
            <div>
              <p className="mb-1 text-zinc-400 dark:text-zinc-500">Signals</p>
              <ul className="space-y-1">
                {signals.map((s, i) => (
                  <li key={i} className="break-words rounded bg-zinc-50 px-2 py-1 font-mono text-[11px] text-zinc-700 dark:bg-zinc-800/60 dark:text-zinc-300">
                    {s}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ResultCard({
  entry,
  onRecheck,
  onWatch,
  onDismiss,
}: {
  entry: Checked;
  onRecheck: () => void;
  onWatch: () => void;
  onDismiss: () => void;
}) {
  const { url, data } = entry;
  const now = useNow();
  const status = asStatus(data.status);
  const title = data.name || hostOf(url);
  const statusText = data.status_text && data.status_text !== statusMeta[status].label ? data.status_text : null;
  return (
    <div className="rounded-xl border border-zinc-200 p-3 sm:p-4 dark:border-zinc-800" data-testid="quick-check-result">
      <div className="flex gap-3 sm:gap-4">
        <a href={url} target="_blank" rel="noreferrer noopener" className="shrink-0" tabIndex={-1} aria-hidden>
          <ItemImage src={data.image_url} alt={title} className="size-24 rounded-lg ring-1 ring-zinc-900/5 sm:size-32 dark:ring-white/10" />
        </a>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <StoreChip data={data} url={url} />
            <button
              type="button"
              onClick={onDismiss}
              aria-label="Dismiss result"
              className="-mr-1 -mt-1 shrink-0 rounded p-1 text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200"
            >
              <X className="size-4" />
            </button>
          </div>
          <p className="mt-1.5 line-clamp-2 text-sm font-semibold text-zinc-900 sm:text-base dark:text-zinc-50" title={title}>
            {title}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <StatusBadge status={status} className="px-3 py-1 text-sm [&_svg]:size-4" />
            {data.price && <span className="text-lg font-semibold tabular-nums text-zinc-900 dark:text-zinc-50">{data.price}</span>}
            {data.queued && (
              <Badge tone="amber">
                <Clock /> Queue / waiting room
              </Badge>
            )}
          </div>
          {statusText && <p className="mt-1 line-clamp-2 text-xs text-zinc-500 dark:text-zinc-400">{statusText}</p>}
          {data.seller && (
            <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
              <Store className="mr-1 inline size-3 -translate-y-px" />
              Sold by <span className="break-words font-medium text-zinc-800 dark:text-zinc-200">{data.seller}</span>
              {data.third_party && <span className="font-medium text-amber-700 dark:text-amber-400"> · third-party</span>}
            </p>
          )}
        </div>
      </div>

      <div className="mt-3 space-y-2">
        <SupportBox data={data} />
        {data.error && data.support?.detail !== data.error && (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">{data.error}</p>
        )}
        <WhySection data={data} />
      </div>

      <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" onClick={onWatch} className="w-full sm:w-auto">
            <Eye /> Watch this
          </Button>
          <a
            href={url}
            target="_blank"
            rel="noreferrer noopener"
            className={buttonClasses({ variant: 'outline', className: 'flex-1 sm:flex-none' })}
          >
            <ExternalLink /> Open page
          </a>
          {data.cart_url && (
            <a
              href={data.cart_url}
              target="_blank"
              rel="noreferrer noopener"
              className={buttonClasses({ variant: 'outline', className: 'flex-1 sm:flex-none' })}
            >
              <ShoppingCart /> Add to cart
            </a>
          )}
        </div>
        <div className="flex items-center justify-between gap-2 sm:ml-auto sm:justify-end">
          <span className="text-xs text-zinc-500 dark:text-zinc-400">Checked {relativeTime(new Date(entry.checkedAt).toISOString(), now)}</span>
          <Button variant="ghost" size="sm" onClick={onRecheck}>
            <RefreshCw /> Re-check
          </Button>
        </div>
      </div>
    </div>
  );
}

function HistoryList({
  history,
  activeUrl,
  onPick,
  onClear,
}: {
  history: Checked[];
  activeUrl?: string;
  onPick: (h: Checked) => void;
  onClear: () => void;
}) {
  const now = useNow();
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <p className="inline-flex items-center gap-1.5 text-xs font-medium text-zinc-500 dark:text-zinc-400">
          <History className="size-3.5" /> Recent checks
        </p>
        <button type="button" onClick={onClear} className="text-xs text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200">
          Clear
        </button>
      </div>
      <ul className="divide-y divide-zinc-100 rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
        {history.map((h) => {
          const status = asStatus(h.data.status);
          const level = h.data.support?.level;
          return (
            <li key={h.url}>
              <button
                type="button"
                onClick={() => onPick(h)}
                className={cn(
                  'flex w-full items-center gap-2.5 px-2.5 py-2 text-left transition-colors hover:bg-zinc-50 focus-visible:bg-zinc-50 focus-visible:outline-none dark:hover:bg-zinc-800/50 dark:focus-visible:bg-zinc-800/50',
                  activeUrl === h.url && 'bg-zinc-50 dark:bg-zinc-800/50',
                )}
              >
                <ItemImage src={h.data.image_url} alt="" className="size-8 shrink-0 rounded-md" iconClassName="size-4" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm text-zinc-800 dark:text-zinc-200">{h.data.name || hostOf(h.url)}</span>
                  <span className="block truncate text-[11px] text-zinc-500 dark:text-zinc-400">
                    {h.data.retailer?.name ?? hostOf(h.url)}
                    {level && ` · ${supportMeta[level]?.short ?? level}`} · {relativeTime(new Date(h.checkedAt).toISOString(), now)}
                  </span>
                </span>
                <span className={cn('inline-flex shrink-0 items-center gap-1.5 text-xs font-medium', statusMeta[status].text)}>
                  <StatusDot status={status} />
                  <span className="hidden sm:inline">{statusMeta[status].label}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
