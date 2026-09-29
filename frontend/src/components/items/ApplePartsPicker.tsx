import { AlertTriangle, Check, Plus, RefreshCw, X } from 'lucide-react';
import { useState, type KeyboardEvent } from 'react';
import type { ApplePart, AppleVariant } from '@/lib/types';
import { APPLE_PART_RE, cn } from '@/lib/utils';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Skeleton } from '../ui/skeleton';

export function ApplePartsPicker({
  parts,
  onChange,
  variants,
  loading,
  error,
  urlReady,
  invalid,
  onRetry,
}: {
  parts: ApplePart[];
  onChange: (p: ApplePart[]) => void;
  variants: AppleVariant[];
  loading: boolean;
  error: string | null;
  urlReady: boolean;
  invalid?: string;
  onRetry: () => void;
}) {
  const [manual, setManual] = useState('');
  const [manualErr, setManualErr] = useState<string | null>(null);
  const selected = new Set(parts.map((p) => p.part_number.toUpperCase()));
  const variantNums = new Set(variants.map((v) => v.part_number.toUpperCase()));
  const extra = parts.filter((p) => !variantNums.has(p.part_number.toUpperCase()));

  const toggle = (v: AppleVariant) => {
    const key = v.part_number.toUpperCase();
    if (selected.has(key)) onChange(parts.filter((p) => p.part_number.toUpperCase() !== key));
    else onChange([...parts, { part_number: v.part_number, label: v.label }]);
  };

  const addManual = () => {
    const pn = manual.trim().toUpperCase();
    if (!pn) return;
    if (!APPLE_PART_RE.test(pn)) {
      setManualErr('That doesn’t look like an Apple part number (e.g. MG8H4LL/A).');
      return;
    }
    if (selected.has(pn)) {
      setManualErr('Already added.');
      return;
    }
    const v = variants.find((x) => x.part_number.toUpperCase() === pn);
    onChange([...parts, { part_number: pn, label: v?.label ?? pn }]);
    setManual('');
    setManualErr(null);
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addManual();
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium text-zinc-800 dark:text-zinc-200">Models to watch</p>
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            {parts.length ? `${parts.length} selected` : 'Pick one or more configurations.'}
          </p>
        </div>
        {urlReady && !loading && (
          <Button size="xs" variant="ghost" onClick={onRetry}>
            <RefreshCw /> Reload
          </Button>
        )}
      </div>

      {loading ? (
        <div className="grid gap-2 sm:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[58px] rounded-xl" />
          ))}
        </div>
      ) : variants.length ? (
        <div className="grid max-h-[22rem] gap-2 overflow-y-auto p-0.5 sm:grid-cols-2" role="group" aria-label="Available models">
          {variants.map((v) => {
            const on = selected.has(v.part_number.toUpperCase());
            return (
              <button
                key={v.part_number}
                type="button"
                role="checkbox"
                aria-checked={on}
                onClick={() => toggle(v)}
                className={cn(
                  'flex items-start gap-3 rounded-xl border p-3 text-left transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60',
                  on
                    ? 'border-indigo-500/60 bg-indigo-50/70 ring-1 ring-indigo-500/30 dark:border-indigo-400/50 dark:bg-indigo-500/10'
                    : 'border-zinc-200 hover:border-zinc-300 hover:bg-zinc-50 dark:border-zinc-800 dark:hover:border-zinc-700 dark:hover:bg-zinc-800/40',
                )}
              >
                <span
                  className={cn(
                    'mt-0.5 flex size-4 shrink-0 items-center justify-center rounded border transition-colors',
                    on ? 'border-indigo-600 bg-indigo-600 text-white dark:border-indigo-500 dark:bg-indigo-500' : 'border-zinc-300 dark:border-zinc-600',
                  )}
                >
                  {on && <Check className="size-3" strokeWidth={3} />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium leading-snug text-zinc-900 dark:text-zinc-100">{v.label}</span>
                  <span className="mt-0.5 flex items-center justify-between gap-2 text-xs text-zinc-500 dark:text-zinc-400">
                    <span className="font-mono">{v.part_number}</span>
                    {v.price && <span className="tabular-nums">{v.price}</span>}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <div className="flex items-start gap-2 rounded-xl border border-dashed border-zinc-300 px-3 py-3 text-sm text-zinc-500 dark:border-zinc-700 dark:text-zinc-400">
          {error ? <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-500" /> : null}
          <span>
            {!urlReady
              ? 'Paste an apple.com product URL above to load its models.'
              : error
                ? `Couldn't load models (${error}). Add part numbers manually below.`
                : 'No models found on this page. Add part numbers manually below.'}
          </span>
        </div>
      )}

      {extra.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {extra.map((p) => (
            <span
              key={p.part_number}
              className="inline-flex items-center gap-1.5 rounded-lg border border-indigo-500/40 bg-indigo-50 py-1 pl-2.5 pr-1 text-xs font-medium text-indigo-800 dark:bg-indigo-500/10 dark:text-indigo-200"
            >
              <span className="font-mono">{p.part_number}</span>
              {p.label && p.label !== p.part_number && <span className="text-indigo-600/70 dark:text-indigo-300/70">{p.label}</span>}
              <button
                type="button"
                onClick={() => onChange(parts.filter((x) => x.part_number !== p.part_number))}
                className="rounded p-0.5 hover:bg-indigo-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 dark:hover:bg-indigo-500/20"
                aria-label={`Remove ${p.part_number}`}
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="space-y-1.5">
        <div className="flex gap-2">
          <Input
            placeholder="Add part number, e.g. MG8H4LL/A"
            value={manual}
            onChange={(e) => {
              setManual(e.target.value);
              setManualErr(null);
            }}
            onKeyDown={onKey}
            className="font-mono text-[13px] uppercase placeholder:font-sans placeholder:normal-case"
            autoCapitalize="characters"
            spellCheck={false}
            aria-label="Apple part number"
            invalid={!!manualErr}
          />
          <Button onClick={addManual} disabled={!manual.trim()}>
            <Plus /> Add
          </Button>
        </div>
        {manualErr ? (
          <p className="text-xs text-rose-600 dark:text-rose-400">{manualErr}</p>
        ) : invalid ? (
          <p className="text-xs text-rose-600 dark:text-rose-400">{invalid}</p>
        ) : (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            Part numbers are shown on apple.com under “Compare” / in the bag, and end in something like LL/A.
          </p>
        )}
      </div>
    </div>
  );
}
