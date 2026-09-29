import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function parseDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  // Be lenient: treat naive timestamps as UTC.
  const s = /[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`;
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  const d = parseDate(iso);
  if (!d) return 'never';
  const diff = Math.round((now - d.getTime()) / 1000);
  const abs = Math.abs(diff);
  const future = diff < 0;
  let out: string;
  if (abs < 10) return 'just now';
  if (abs < 60) out = `${abs}s`;
  else if (abs < 3600) out = `${Math.floor(abs / 60)}m`;
  else if (abs < 86400) out = `${Math.floor(abs / 3600)}h`;
  else if (abs < 86400 * 30) out = `${Math.floor(abs / 86400)}d`;
  else return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  return future ? `in ${out}` : `${out} ago`;
}

export function absoluteTime(iso: string | null | undefined): string {
  const d = parseDate(iso);
  if (!d) return '';
  return d.toLocaleString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

export function isAppleUrl(url: string): boolean {
  try {
    const h = new URL(url).hostname.toLowerCase();
    return h === 'apple.com' || h.endsWith('.apple.com');
  } catch {
    return false;
  }
}

export function isValidUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch {
    return false;
  }
}

export function formatInterval(minutes: number): string {
  if (minutes < 1) return `${Math.round(minutes * 60)}s`;
  if (minutes < 60) return `${minutes} min`;
  const h = minutes / 60;
  return Number.isInteger(h) ? `${h} h` : `${h.toFixed(1)} h`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '';
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** Loose Apple part-number check, e.g. MG8H4LL/A, MXK23AM/A, Z1FG */
export const APPLE_PART_RE = /^[A-Z0-9]{4,6}[A-Z]{1,3}\/[A-Z]$|^Z[A-Z0-9]{3,}$/i;

function parseAmount(token: string): number | null {
  let s = token;
  if (s.includes(',') && s.includes('.')) s = s.lastIndexOf(',') > s.lastIndexOf('.') ? s.replace(/\./g, '').replace(',', '.') : s.replace(/,/g, '');
  else if (s.includes(',')) s = /,\d{3}$/.test(s) ? s.replace(/,/g, '') : s.replace(',', '.');
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/** "$1,099.00" → 1099; ranges ("$19.99 - $29.99") → the lowest amount (null when there's no number). */
export function parsePrice(price: string | null | undefined): number | null {
  if (!price) return null;
  const amounts = (price.match(/\d(?:[\d.,]*\d)?/g) ?? []).map(parseAmount).filter((n): n is number => n != null);
  return amounts.length ? Math.min(...amounts) : null;
}

const LIMIT_NOTE_RE = /\s*·\s*above your (\S+) limit$/i;

/** Split the checker's " · above your $75 limit" suffix off a status text. */
export function splitLimitNote(text: string | null | undefined): { text: string; limit: string | null } {
  const t = text ?? '';
  const m = LIMIT_NOTE_RE.exec(t);
  return m ? { text: t.slice(0, m.index), limit: m[1] } : { text: t, limit: null };
}

/** 50 → "$50", 49.99 → "$49.99" */
export function formatMoney(n: number): string {
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 })}`;
}

/** Price is known and above the item's limit. */
export function isAboveLimit(item: { price: string | null; max_price?: number | null }): boolean {
  const p = parsePrice(item.price);
  return item.max_price != null && p != null && p > item.max_price;
}
