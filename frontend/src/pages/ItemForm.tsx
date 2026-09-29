import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  ArrowLeft,
  Bell,
  Clock,
  Code2,
  Globe,
  ImagePlus,
  Link2,
  Loader2,
  MapPin,
  Package,
  RefreshCw,
  Save,
  ScanSearch,
  Sparkles,
  Store,
  TextSearch,
  Trash2,
  Truck,
  Upload,
  Wand2,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import { ApplePartsPicker } from '@/components/items/ApplePartsPicker';
import { ItemImage } from '@/components/ItemImage';
import { AppleLogo } from '@/components/Logo';
import { PageHeader } from '@/components/PageHeader';
import { PageLoader } from '@/components/PageLoader';
import { StatusBadge } from '@/components/StatusBadge';
import { Button, buttonClasses } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Field, Input } from '@/components/ui/input';
import { Segmented } from '@/components/ui/segmented';
import { SwitchRow } from '@/components/ui/switch';
import { useDebounce } from '@/hooks/useDebounce';
import { upsertItem, useItemActions } from '@/hooks/useItemActions';
import { api, ApiError, errorMessage } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import type { AppleConfig, ApplePart, GenericConfig, GenericMode, Item, ItemCreate, ItemKind, Settings } from '@/lib/types';
import { cn, formatInterval, hostOf, isAppleUrl, isValidUrl } from '@/lib/utils';

const INTERVAL_PRESETS = [1, 2, 5, 15, 60];
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

const FALLBACK_SETTINGS: Pick<Settings, 'default_interval_minutes' | 'default_zip' | 'default_max_distance_miles'> = {
  default_interval_minutes: 2,
  default_zip: null,
  default_max_distance_miles: 25,
};

export default function ItemFormPage() {
  const { id } = useParams();
  const itemId = id ? Number(id) : null;
  const settings = useQuery({ queryKey: qk.settings, queryFn: api.settings, staleTime: 60_000 });
  const item = useQuery({
    queryKey: qk.item(itemId ?? 0),
    queryFn: () => api.item(itemId!),
    enabled: itemId != null && Number.isFinite(itemId),
  });

  if (itemId != null) {
    if (item.isLoading) return <PageLoader />;
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
  }
  if (settings.isLoading) return <PageLoader />;

  return (
    <ItemFormInner
      key={item.data?.id ?? 'new'}
      item={item.data ?? null}
      defaults={settings.data ?? FALLBACK_SETTINGS}
    />
  );
}

interface FormState {
  url: string;
  name: string;
  kind: ItemKind;
  interval: string;
  notify: boolean;
  generic: GenericConfig;
  apple: AppleConfig;
}

function initialState(item: Item | null, d: typeof FALLBACK_SETTINGS): FormState {
  return {
    url: item?.url ?? '',
    name: item?.name ?? '',
    kind: item?.kind ?? 'generic',
    interval: String(item?.interval_minutes ?? d.default_interval_minutes ?? 2),
    notify: item?.notify_enabled ?? true,
    generic: {
      mode: item?.generic_config?.mode ?? 'auto',
      selector: item?.generic_config?.selector ?? null,
      in_stock_text: item?.generic_config?.in_stock_text ?? null,
      out_of_stock_text: item?.generic_config?.out_of_stock_text ?? null,
      render_js: item?.generic_config?.render_js ?? false,
    },
    apple: {
      parts: item?.apple_config?.parts ?? [],
      zip: item?.apple_config?.zip ?? d.default_zip ?? '',
      max_distance_miles: item?.apple_config?.max_distance_miles ?? d.default_max_distance_miles ?? 25,
      watch_pickup: item?.apple_config?.watch_pickup ?? true,
      watch_delivery: item?.apple_config?.watch_delivery ?? true,
    },
  };
}

function ItemFormInner({ item, defaults }: { item: Item | null; defaults: typeof FALLBACK_SETTINGS }) {
  const isEdit = !!item;
  const navigate = useNavigate();
  const qc = useQueryClient();
  const actions = useItemActions();
  const [s, setS] = useState<FormState>(() => initialState(item, defaults));
  const [nameTouched, setNameTouched] = useState(isEdit);
  const [kindTouched, setKindTouched] = useState(isEdit);
  const [submitted, setSubmitted] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [filePreview, setFilePreview] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setS((p) => ({ ...p, [k]: v }));
  const setGeneric = (patch: Partial<GenericConfig>) => setS((p) => ({ ...p, generic: { ...p.generic, ...patch } }));
  const setApple = (patch: Partial<AppleConfig>) => setS((p) => ({ ...p, apple: { ...p.apple, ...patch } }));

  const url = s.url.trim();
  const urlValid = isValidUrl(url);
  const debouncedUrl = useDebounce(url, 600);
  const urlChanged = !isEdit || debouncedUrl !== item?.url;

  // --- Preview (autofill) -------------------------------------------------
  const preview = useQuery({
    queryKey: ['preview', debouncedUrl],
    queryFn: ({ signal }) => api.preview(debouncedUrl, signal),
    enabled: isValidUrl(debouncedUrl) && urlChanged,
    staleTime: 5 * 60_000,
    retry: false,
  });

  useEffect(() => {
    const p = preview.data;
    if (!p) return;
    if (!nameTouched && p.name) setS((prev) => ({ ...prev, name: p.name ?? prev.name }));
    if (!kindTouched && p.is_apple) setS((prev) => ({ ...prev, kind: 'apple' }));
  }, [preview.data]);

  const appleUrl = isAppleUrl(url);
  const showKindToggle = appleUrl || s.kind === 'apple' || !!preview.data?.is_apple;

  // --- Apple variants -----------------------------------------------------
  const resolve = useQuery({
    queryKey: ['apple-resolve', debouncedUrl],
    queryFn: ({ signal }) => api.appleResolve(debouncedUrl, signal),
    enabled: s.kind === 'apple' && isValidUrl(debouncedUrl),
    staleTime: 10 * 60_000,
    retry: false,
  });

  useEffect(() => {
    if (!nameTouched && resolve.data?.product_name && !s.name)
      setS((prev) => ({ ...prev, name: resolve.data?.product_name ?? prev.name }));
  }, [resolve.data]);

  // --- Photo ---------------------------------------------------------------
  useEffect(() => {
    if (!file) {
      setFilePreview(null);
      return;
    }
    const u = URL.createObjectURL(file);
    setFilePreview(u);
    return () => URL.revokeObjectURL(u);
  }, [file]);

  const autoImage = urlChanged ? preview.data?.image_url ?? resolve.data?.image_url ?? null : null;
  const shownImage = filePreview ?? (isEdit && !urlChanged ? item?.image_url : autoImage) ?? item?.image_url ?? null;

  const onPickFile = (f: File | undefined) => {
    if (!f) return;
    if (!/^image\/(jpeg|png|webp|gif)$/.test(f.type)) {
      toast.error('Unsupported image type', { description: 'Use JPG, PNG, WebP or GIF.' });
      return;
    }
    if (f.size > MAX_IMAGE_BYTES) {
      toast.error('Image too large', { description: 'Maximum size is 8 MB.' });
      return;
    }
    setFile(f);
  };

  // --- Validation ----------------------------------------------------------
  const intervalNum = Number(s.interval);
  const errors = useMemo(() => {
    const e: Partial<Record<'url' | 'interval' | 'selector' | 'text' | 'parts' | 'zip' | 'watch' | 'distance', string>> = {};
    if (!url) e.url = 'Paste a product URL';
    else if (!urlValid) e.url = 'Enter a valid http(s) URL';
    if (!Number.isFinite(intervalNum) || intervalNum < 1) e.interval = 'Minimum is 1 minute';
    else if (intervalNum > 1440) e.interval = 'Maximum is 1440 minutes (24 h)';
    if (s.kind === 'generic') {
      if (s.generic.mode === 'selector' && !s.generic.selector?.trim()) e.selector = 'Enter a CSS selector';
      if (s.generic.mode === 'text' && !s.generic.in_stock_text?.trim() && !s.generic.out_of_stock_text?.trim())
        e.text = 'Enter at least one phrase';
    } else {
      if (!s.apple.parts.length) e.parts = 'Select at least one model';
      if (!s.apple.zip.trim()) e.zip = 'ZIP code is required for store availability';
      if (!s.apple.watch_pickup && !s.apple.watch_delivery) e.watch = 'Watch pickup, delivery, or both';
      if (!(s.apple.max_distance_miles > 0)) e.distance = 'Enter a distance';
    }
    return e;
  }, [url, urlValid, intervalNum, s]);
  const hasErrors = Object.keys(errors).length > 0;
  const show = (k: keyof typeof errors) => (submitted ? errors[k] : undefined);

  // --- Save ----------------------------------------------------------------
  const cleanGeneric = (): GenericConfig => ({
    mode: s.generic.mode,
    selector: s.generic.selector?.trim() || null,
    in_stock_text: s.generic.in_stock_text?.trim() || null,
    out_of_stock_text: s.generic.out_of_stock_text?.trim() || null,
    render_js: s.generic.render_js,
  });
  const cleanApple = (): AppleConfig => ({
    ...s.apple,
    zip: s.apple.zip.trim(),
    max_distance_miles: Number(s.apple.max_distance_miles),
  });

  const save = useMutation({
    mutationFn: async () => {
      let saved: Item;
      if (isEdit && item) {
        saved = await api.updateItem(item.id, {
          name: s.name.trim() || item.name,
          url,
          kind: s.kind,
          interval_minutes: intervalNum,
          notify_enabled: s.notify,
          ...(s.kind === 'apple' ? { apple_config: cleanApple() } : { generic_config: cleanGeneric() }),
        });
      } else {
        const body: ItemCreate = {
          url,
          kind: s.kind,
          interval_minutes: intervalNum,
          notify_enabled: s.notify,
          ...(s.name.trim() ? { name: s.name.trim() } : {}),
          ...(!file && autoImage ? { image_url: autoImage } : {}),
          ...(s.kind === 'apple' ? { apple_config: cleanApple() } : { generic_config: cleanGeneric() }),
        };
        saved = await api.createItem(body);
      }
      if (file) {
        try {
          saved = await api.uploadImage(saved.id, file);
        } catch (e) {
          toast.error('Item saved, but the photo upload failed', { description: errorMessage(e) });
        }
      }
      return saved;
    },
    onSuccess: (saved) => {
      upsertItem(qc, saved);
      qc.invalidateQueries({ queryKey: qk.items });
      qc.invalidateQueries({ queryKey: qk.stats });
      toast.success(isEdit ? 'Changes saved' : 'Now watching', { description: saved.name });
      navigate(`/items/${saved.id}`, { replace: !isEdit ? false : true });
    },
    onError: (e) => toast.error(isEdit ? "Couldn't save changes" : "Couldn't add item", { description: errorMessage(e) }),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (hasErrors) {
      toast.error('Please fix the highlighted fields');
      return;
    }
    save.mutate();
  };

  const previewStatus = urlChanged ? preview.data?.status : item?.status;
  const previewPrice = urlChanged ? preview.data?.price : item?.price;

  return (
    <form onSubmit={onSubmit} noValidate>
      <Link
        to={isEdit ? `/items/${item!.id}` : '/'}
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-zinc-500 transition-colors hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
      >
        <ArrowLeft className="size-4" /> {isEdit ? 'Back to item' : 'Dashboard'}
      </Link>
      <PageHeader
        title={isEdit ? 'Edit item' : 'Add item'}
        description={isEdit ? item!.name : 'Paste a product link — we’ll figure out the rest.'}
      />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="space-y-6">
          {/* Product */}
          <Card>
            <CardHeader icon={<Link2 />} title="Product" description="Link to the product page you want to watch." />
            <CardBody className="space-y-5">
              <Field
                label="Product URL"
                htmlFor="url"
                error={show('url')}
                aside={
                  urlValid && preview.isFetching ? (
                    <span className="inline-flex items-center gap-1.5 text-xs text-zinc-500">
                      <Loader2 className="size-3 animate-spin" /> Fetching page…
                    </span>
                  ) : preview.data?.error && urlChanged ? (
                    <span className="inline-flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
                      <AlertTriangle className="size-3" /> Partial preview
                    </span>
                  ) : preview.data && urlChanged ? (
                    <span className="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
                      <Sparkles className="size-3" /> Autofilled
                    </span>
                  ) : preview.isError ? (
                    <span className="inline-flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
                      <AlertTriangle className="size-3" /> Couldn't preview
                    </span>
                  ) : null
                }
              >
                <Input
                  id="url"
                  type="url"
                  inputMode="url"
                  autoComplete="off"
                  autoFocus={!isEdit}
                  placeholder="https://www.example.com/product/…"
                  value={s.url}
                  onChange={(e) => set('url', e.target.value)}
                  onPaste={(e) => {
                    const t = e.clipboardData.getData('text').trim();
                    if (t) {
                      e.preventDefault();
                      set('url', t);
                    }
                  }}
                  leading={<Globe />}
                  invalid={!!show('url')}
                />
              </Field>
              {urlChanged && (preview.isError || preview.data?.error) && (
                <p className="-mt-3 text-xs text-zinc-500 dark:text-zinc-400">
                  {preview.isError ? errorMessage(preview.error) : preview.data?.error} — you can still save it and fill in the
                  details manually.
                </p>
              )}

              <Field label="Name" htmlFor="name" hint={!isEdit ? 'Leave blank to use the page title.' : undefined}>
                <Input
                  id="name"
                  placeholder={preview.isFetching ? 'Detecting…' : 'e.g. PlayStation 5 Pro'}
                  value={s.name}
                  onChange={(e) => {
                    setNameTouched(true);
                    set('name', e.target.value);
                  }}
                />
              </Field>

              {showKindToggle && (
                <div className="space-y-2">
                  <p className="text-sm font-medium text-zinc-800 dark:text-zinc-200">Watch mode</p>
                  <Segmented<ItemKind>
                    ariaLabel="Watch mode"
                    value={s.kind}
                    onChange={(k) => {
                      setKindTouched(true);
                      set('kind', k);
                    }}
                    options={[
                      { value: 'apple', label: 'Apple Store', icon: <AppleLogo className="!size-3.5" /> },
                      { value: 'generic', label: 'Web page', icon: <Globe /> },
                    ]}
                  />
                  <p className="text-xs text-zinc-500 dark:text-zinc-400">
                    {s.kind === 'apple'
                      ? 'Checks in-store pickup near your ZIP and 2-hour courier delivery for specific models.'
                      : 'Checks the page itself for an in-stock signal.'}
                  </p>
                </div>
              )}
            </CardBody>
          </Card>

          {/* Detection */}
          {s.kind === 'apple' ? (
            <Card>
              <CardHeader
                icon={<AppleLogo className="!size-4" />}
                title="Apple availability"
                description="Choose the exact models to watch and where you can pick them up."
              />
              <CardBody className="space-y-6">
                <ApplePartsPicker
                  parts={s.apple.parts}
                  onChange={(parts: ApplePart[]) => setApple({ parts })}
                  variants={resolve.data?.variants ?? []}
                  loading={resolve.isFetching}
                  error={resolve.isError ? errorMessage(resolve.error) : resolve.data?.error ?? null}
                  urlReady={isValidUrl(debouncedUrl)}
                  invalid={show('parts')}
                  onRetry={() => resolve.refetch()}
                />

                <div className="grid gap-5 sm:grid-cols-2">
                  <Field label="ZIP code" htmlFor="zip" error={show('zip')} hint="Stores are searched around this ZIP.">
                    <Input
                      id="zip"
                      inputMode="numeric"
                      autoComplete="postal-code"
                      placeholder="95014"
                      maxLength={10}
                      value={s.apple.zip}
                      onChange={(e) => setApple({ zip: e.target.value })}
                      leading={<MapPin />}
                      invalid={!!show('zip')}
                    />
                  </Field>
                  <Field
                    label="Max distance"
                    htmlFor="distance"
                    error={show('distance')}
                    aside={
                      <span className="text-sm font-medium tabular-nums text-zinc-900 dark:text-zinc-100">
                        {s.apple.max_distance_miles} mi
                      </span>
                    }
                  >
                    <div className="flex h-9 items-center gap-3">
                      <input
                        id="distance"
                        type="range"
                        min={1}
                        max={100}
                        step={1}
                        value={s.apple.max_distance_miles}
                        onChange={(e) => setApple({ max_distance_miles: Number(e.target.value) })}
                        className="sw-range"
                      />
                      <Input
                        type="number"
                        min={1}
                        max={500}
                        aria-label="Max distance in miles"
                        value={s.apple.max_distance_miles}
                        onChange={(e) => setApple({ max_distance_miles: Number(e.target.value) })}
                        className="w-20 text-right tabular-nums"
                      />
                    </div>
                  </Field>
                </div>

                <div className="space-y-3 rounded-xl border border-zinc-200 p-4 dark:border-zinc-800">
                  <SwitchRow
                    id="watch-pickup"
                    icon={<Store />}
                    title="In-store pickup"
                    description="Alert when any selected model can be picked up at a nearby store."
                    checked={s.apple.watch_pickup}
                    onCheckedChange={(v) => setApple({ watch_pickup: v })}
                  />
                  <div className="h-px bg-zinc-100 dark:bg-zinc-800" />
                  <SwitchRow
                    id="watch-delivery"
                    icon={<Truck />}
                    title="2-hour delivery"
                    description="Alert when courier delivery within 2 hours becomes available."
                    checked={s.apple.watch_delivery}
                    onCheckedChange={(v) => setApple({ watch_delivery: v })}
                  />
                  {show('watch') && <p className="text-xs text-rose-600 dark:text-rose-400">{show('watch')}</p>}
                </div>
              </CardBody>
            </Card>
          ) : (
            <Card>
              <CardHeader
                icon={<ScanSearch />}
                title="Detection"
                description="How Stock Watcher decides whether the product is available."
              />
              <CardBody className="space-y-5">
                <Segmented<GenericMode>
                  ariaLabel="Detection mode"
                  value={s.generic.mode}
                  onChange={(mode) => setGeneric({ mode })}
                  options={[
                    { value: 'auto', label: 'Auto', icon: <Wand2 /> },
                    { value: 'selector', label: 'CSS selector', icon: <Code2 /> },
                    { value: 'text', label: 'Text match', icon: <TextSearch /> },
                  ]}
                />
                {s.generic.mode === 'auto' && (
                  <p className="rounded-lg bg-zinc-50 px-3 py-2.5 text-sm text-zinc-600 dark:bg-zinc-800/40 dark:text-zinc-400">
                    Reads structured data (schema.org availability, meta tags) and common "Add to cart" / "Sold out" buttons.
                    Works for most stores — switch modes only if detection is wrong.
                  </p>
                )}
                {s.generic.mode === 'selector' && (
                  <div className="space-y-4">
                    <Field
                      label="CSS selector"
                      htmlFor="selector"
                      error={show('selector')}
                      hint="The item counts as in stock when this element exists (and matches the text below, if given)."
                    >
                      <Input
                        id="selector"
                        placeholder="button.add-to-cart:not([disabled])"
                        value={s.generic.selector ?? ''}
                        onChange={(e) => setGeneric({ selector: e.target.value })}
                        className="font-mono text-[13px]"
                        spellCheck={false}
                        autoCapitalize="none"
                        invalid={!!show('selector')}
                      />
                    </Field>
                    <Field label="In-stock text (optional)" htmlFor="sel-in" hint="Only count as in stock if the element contains this text.">
                      <Input
                        id="sel-in"
                        placeholder="Add to cart"
                        value={s.generic.in_stock_text ?? ''}
                        onChange={(e) => setGeneric({ in_stock_text: e.target.value })}
                      />
                    </Field>
                  </div>
                )}
                {s.generic.mode === 'text' && (
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field label="In-stock phrase" htmlFor="txt-in" hint='Page contains this → in stock. e.g. "Add to cart"'>
                      <Input
                        id="txt-in"
                        placeholder="Add to cart"
                        value={s.generic.in_stock_text ?? ''}
                        onChange={(e) => setGeneric({ in_stock_text: e.target.value })}
                        invalid={!!show('text')}
                      />
                    </Field>
                    <Field label="Out-of-stock phrase" htmlFor="txt-out" hint='Page contains this → out of stock. e.g. "Sold out"'>
                      <Input
                        id="txt-out"
                        placeholder="Sold out"
                        value={s.generic.out_of_stock_text ?? ''}
                        onChange={(e) => setGeneric({ out_of_stock_text: e.target.value })}
                        invalid={!!show('text')}
                      />
                    </Field>
                    {show('text') && <p className="text-xs text-rose-600 sm:col-span-2 dark:text-rose-400">{show('text')}</p>}
                  </div>
                )}
                <div className="rounded-xl border border-zinc-200 p-4 dark:border-zinc-800">
                  <SwitchRow
                    id="render-js"
                    icon={<Code2 />}
                    title="Render JavaScript"
                    description="Load the page in a headless browser. Slower — enable only for sites that build the page client-side."
                    checked={s.generic.render_js}
                    onCheckedChange={(v) => setGeneric({ render_js: v })}
                  />
                </div>
              </CardBody>
            </Card>
          )}

          {/* Schedule */}
          <Card>
            <CardHeader icon={<Clock />} title="Schedule & alerts" />
            <CardBody className="space-y-5">
              <Field label="Check every" htmlFor="interval" error={show('interval')}>
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <div className="relative w-full sm:w-36">
                    <Input
                      id="interval"
                      type="number"
                      inputMode="numeric"
                      min={1}
                      max={1440}
                      value={s.interval}
                      onChange={(e) => set('interval', e.target.value)}
                      className="pr-14 tabular-nums"
                      invalid={!!show('interval')}
                    />
                    <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm text-zinc-400">min</span>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {INTERVAL_PRESETS.map((m) => (
                      <button
                        key={m}
                        type="button"
                        onClick={() => set('interval', String(m))}
                        className={cn(
                          'h-8 rounded-lg border px-2.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60',
                          Number(s.interval) === m
                            ? 'border-indigo-500/50 bg-indigo-50 text-indigo-700 dark:bg-indigo-500/10 dark:text-indigo-300'
                            : 'border-zinc-200 text-zinc-600 hover:border-zinc-300 hover:text-zinc-900 dark:border-zinc-800 dark:text-zinc-400 dark:hover:border-zinc-700 dark:hover:text-zinc-100',
                        )}
                      >
                        {formatInterval(m)}
                      </button>
                    ))}
                  </div>
                </div>
              </Field>
              <SwitchRow
                id="notify"
                icon={<Bell />}
                title="Send notifications"
                description="Push an ntfy alert when this item comes back in stock. Alerts always appear in the in-app notification center."
                checked={s.notify}
                onCheckedChange={(v) => set('notify', v)}
              />
            </CardBody>
          </Card>
        </div>

        {/* Preview / photo */}
        <div className="lg:sticky lg:top-24 lg:self-start">
          <Card className="overflow-hidden">
            <div className="relative">
              <ItemImage src={shownImage} alt={s.name || 'Product photo'} className="aspect-square w-full" iconClassName="size-12" />
              {(preview.isFetching || (s.kind === 'apple' && resolve.isFetching && !shownImage)) && (
                <div className="absolute inset-0 flex items-center justify-center bg-white/40 backdrop-blur-[1px] dark:bg-zinc-950/40">
                  <Loader2 className="size-6 animate-spin text-zinc-400" />
                </div>
              )}
              {filePreview && (
                <span className="absolute left-3 top-3 rounded-full bg-indigo-600 px-2 py-0.5 text-[11px] font-medium text-white shadow">
                  Custom photo
                </span>
              )}
            </div>
            <div className="space-y-3 border-t border-zinc-100 p-4 dark:border-zinc-800">
              <div>
                <p className="line-clamp-2 text-sm font-semibold text-zinc-900 dark:text-zinc-50">
                  {s.name || (preview.isFetching ? 'Detecting product…' : 'Untitled product')}
                </p>
                <p className="mt-0.5 truncate text-xs text-zinc-500 dark:text-zinc-400">{urlValid ? hostOf(url) : 'No URL yet'}</p>
              </div>
              {(previewStatus || previewPrice) && (
                <div className="flex items-center justify-between gap-2">
                  {previewStatus ? <StatusBadge status={previewStatus} size="sm" /> : <span />}
                  {previewPrice && <span className="text-sm font-semibold tabular-nums">{previewPrice}</span>}
                </div>
              )}
              <input
                ref={fileInput}
                type="file"
                accept="image/jpeg,image/png,image/webp,image/gif"
                className="hidden"
                onChange={(e) => {
                  onPickFile(e.target.files?.[0]);
                  e.target.value = '';
                }}
              />
              <div className="flex flex-wrap gap-2 pt-1">
                <Button size="sm" onClick={() => fileInput.current?.click()}>
                  {file ? <ImagePlus /> : <Upload />} {file ? 'Change photo' : 'Upload photo'}
                </Button>
                {file && (
                  <Button size="sm" variant="ghost" onClick={() => setFile(null)}>
                    <Trash2 /> {isEdit ? 'Discard' : 'Use auto'}
                  </Button>
                )}
                {isEdit && !file && (
                  <Button
                    size="sm"
                    variant="ghost"
                    loading={actions.refreshImage.isPending}
                    onClick={() => actions.refreshImage.mutate(item!)}
                  >
                    {!actions.refreshImage.isPending && <RefreshCw />} Refresh
                  </Button>
                )}
              </div>
              <p className="text-xs text-zinc-500 dark:text-zinc-400">
                {file
                  ? 'Your photo will be uploaded when you save.'
                  : isEdit
                    ? 'Upload your own or re-fetch it from the product page.'
                    : 'Photo is taken from the product page automatically.'}
              </p>
            </div>
          </Card>
        </div>
      </div>

      {/* Sticky action bar */}
      <div className="sticky bottom-[calc(4rem+env(safe-area-inset-bottom))] z-20 -mx-4 mt-6 border-t border-zinc-200 bg-zinc-50/85 px-4 py-3 backdrop-blur-xl sm:-mx-6 sm:px-6 lg:bottom-0 lg:-mx-8 lg:px-8 dark:border-zinc-800 dark:bg-zinc-950/85">
        <div className="flex items-center justify-end gap-2">
          {submitted && hasErrors && (
            <span className="mr-auto hidden items-center gap-1.5 text-sm text-rose-600 sm:inline-flex dark:text-rose-400">
              <AlertTriangle className="size-4" /> Fix the highlighted fields
            </span>
          )}
          <Button variant="ghost" onClick={() => navigate(-1)}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={save.isPending}>
            {!save.isPending && (isEdit ? <Save /> : <Package />)}
            {isEdit ? 'Save changes' : 'Start watching'}
          </Button>
        </div>
      </div>
    </form>
  );
}
