import { useQuery } from '@tanstack/react-query';
import { api } from './api';
import { qk } from './queryClient';
import type { Retailer } from './types';

/** The supported-store registry. Rarely changes: cache for the session. */
export function useRetailers() {
  return useQuery({ queryKey: qk.retailers, queryFn: api.retailers, staleTime: Infinity, gcTime: Infinity });
}

/** Client-side host match against each store's hosts, most specific host wins (mirrors the server). */
export function matchRetailer(retailers: Retailer[] | undefined, url: string): Retailer | null {
  if (!retailers?.length) return null;
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase().replace(/^www\./, '');
  } catch {
    return null;
  }
  let best: Retailer | null = null;
  let bestLen = 0;
  for (const r of retailers) {
    for (const h of r.hosts?.length ? r.hosts : [r.domain]) {
      const d = h.toLowerCase();
      if ((host === d || host.endsWith(`.${d}`)) && d.length > bestLen) {
        best = r;
        bestLen = d.length;
      }
    }
  }
  return best;
}

/** "Best Buy" → "BB", "Target" → "Ta", "B&H Photo Video" → "BP". */
export function monogram(name: string): string {
  const words = name.replace(/[^\p{L}\p{N}\s]/gu, ' ').split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  const w = words[0] ?? name;
  return w.slice(0, 1).toUpperCase() + w.slice(1, 2).toLowerCase();
}

export const fulfillmentLabel = { delivery: 'Delivery', pickup: 'Pickup', any: 'Delivery or pickup' } as const;
