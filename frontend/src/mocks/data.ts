import type { CheckEvent, Item, ItemStatus, Notification, Settings, User } from '@/lib/types';
import { IMG } from './images';

const now = Date.now();
export const ago = (ms: number) => new Date(now - ms).toISOString().replace(/\.\d+Z$/, 'Z');
const m = 60_000;
const h = 60 * m;

export const users: User[] = [
  { id: 1, username: 'carlos', is_admin: true, created_at: ago(40 * 24 * h) },
  { id: 2, username: 'maria', is_admin: false, created_at: ago(12 * 24 * h) },
  { id: 3, username: 'dad', is_admin: false, created_at: ago(3 * 24 * h) },
];

export const settings: Settings & { ntfy_token?: string } = {
  ntfy_server: 'https://ntfy.sh',
  ntfy_topic: 'carlos-restocks-x81k',
  ntfy_token_set: true,
  ntfy_priority: 4,
  default_interval_minutes: 2,
  default_zip: '95014',
  default_max_distance_miles: 25,
  notify_on_out_of_stock: false,
  theme: ((): Settings['theme'] => {
    const t = localStorage.getItem('sw-theme');
    return t === 'light' || t === 'system' ? t : 'dark';
  })(),
};

const base = {
  enabled: true,
  notify_enabled: true,
  interval_minutes: 2,
  last_error: null,
  generic_config: { mode: 'auto' as const, selector: null, in_stock_text: null, out_of_stock_text: null, render_js: false },
  apple_config: null,
  created_at: ago(9 * 24 * h),
  updated_at: ago(2 * h),
};

export const items: Item[] = [
  {
    ...base,
    id: 1,
    name: 'iPhone 17 Pro 256GB Cosmic Orange',
    url: 'https://www.apple.com/shop/buy-iphone/iphone-17-pro',
    kind: 'apple',
    image_url: IMG.iphone,
    status: 'in_stock',
    status_text: 'Pickup today at 2 stores · 2-hour delivery',
    price: '$1,099.00',
    last_checked_at: ago(40_000),
    last_change_at: ago(14 * m),
    generic_config: null,
    apple_config: {
      parts: [
        { part_number: 'MG8H4LL/A', label: '256GB Cosmic Orange' },
        { part_number: 'MG8J4LL/A', label: '512GB Cosmic Orange' },
      ],
      zip: '95014',
      max_distance_miles: 25,
      watch_pickup: true,
      watch_delivery: true,
      pickup_today_only: false,
    },
    last_result: {
      stores: [
        {
          store_number: 'R014', name: 'Valley Fair', city: 'Santa Clara', distance_miles: 3.2,
          parts: [
            { part_number: 'MG8H4LL/A', label: '256GB Cosmic Orange', available: true, today: true, quote: 'Available Today' },
            { part_number: 'MG8J4LL/A', label: '512GB Cosmic Orange', available: false, quote: 'Unavailable for pickup' },
          ],
        },
        {
          store_number: 'R085', name: 'Stanford', city: 'Palo Alto', distance_miles: 9.8,
          parts: [
            { part_number: 'MG8H4LL/A', label: '256GB Cosmic Orange', available: true, today: true, quote: 'Available Today' },
            { part_number: 'MG8J4LL/A', label: '512GB Cosmic Orange', available: true, today: false, quote: 'Available Oct 3' },
          ],
        },
        {
          store_number: 'R033', name: 'Los Gatos', city: 'Los Gatos', distance_miles: 7.4,
          parts: [
            { part_number: 'MG8H4LL/A', label: '256GB Cosmic Orange', available: false, quote: 'Unavailable for pickup' },
            { part_number: 'MG8J4LL/A', label: '512GB Cosmic Orange', available: false, quote: 'Unavailable for pickup' },
          ],
        },
        {
          store_number: 'R105', name: 'Hillsdale', city: 'San Mateo', distance_miles: 21.6,
          parts: [
            { part_number: 'MG8H4LL/A', label: '256GB Cosmic Orange', available: false, quote: 'Unavailable for pickup' },
            { part_number: 'MG8J4LL/A', label: '512GB Cosmic Orange', available: false, quote: 'Unavailable for pickup' },
          ],
        },
      ],
      stores_out_of_range: 3,
      zip: '95014',
      delivery: [
        { part_number: 'MG8H4LL/A', label: '256GB Cosmic Orange', two_hour: true, quote: 'Delivers in 2 hours · $9' },
        { part_number: 'MG8J4LL/A', label: '512GB Cosmic Orange', two_hour: false, quote: 'Delivers Oct 3 – Oct 6' },
      ],
    },
  },
  {
    ...base,
    id: 2,
    name: 'PlayStation 5 Pro Console',
    url: 'https://www.bestbuy.com/site/sony-playstation-5-pro-console/6614313.p',
    kind: 'generic',
    image_url: IMG.ps5,
    status: 'out_of_stock',
    status_text: 'Sold Out',
    price: '$699.99',
    last_checked_at: ago(75_000),
    last_change_at: ago(3 * 24 * h),
    last_result: { signals: ['json-ld: OutOfStock', 'button: "Sold Out" (disabled)'], matched: 'Sold Out' },
  },
  {
    ...base,
    id: 3,
    name: 'Nintendo Switch 2 – Mario Kart World Bundle',
    url: 'https://www.target.com/p/nintendo-switch-2-mario-kart-world-bundle/-/A-94693225',
    kind: 'generic',
    image_url: IMG.switch2,
    status: 'in_stock',
    status_text: 'In stock – ships today',
    price: '$499.99',
    interval_minutes: 5,
    last_checked_at: ago(2 * m),
    last_change_at: ago(47 * m),
    last_result: { signals: ['json-ld: InStock', 'meta: product:availability = in stock', 'button: "Add to cart"'], matched: 'Add to cart' },
  },
  {
    ...base,
    id: 4,
    name: 'AirPods Pro 3',
    url: 'https://www.apple.com/shop/product/MFHP4LL/A/airpods-pro-3',
    kind: 'apple',
    image_url: IMG.airpods,
    status: 'out_of_stock',
    status_text: 'Not available nearby',
    price: '$249.00',
    last_checked_at: ago(30_000),
    last_change_at: ago(5 * h),
    generic_config: null,
    apple_config: { parts: [{ part_number: 'MFHP4LL/A', label: 'AirPods Pro 3' }], zip: '95014', max_distance_miles: 15, watch_pickup: true, watch_delivery: false, pickup_today_only: true },
    last_result: {
      stores: [
        { store_number: 'R014', name: 'Valley Fair', city: 'Santa Clara', distance_miles: 3.2, parts: [{ part_number: 'MFHP4LL/A', label: 'AirPods Pro 3', available: false, quote: 'Unavailable for pickup' }] },
        { store_number: 'R033', name: 'Los Gatos', city: 'Los Gatos', distance_miles: 7.4, parts: [{ part_number: 'MFHP4LL/A', label: 'AirPods Pro 3', available: false, quote: 'Unavailable for pickup' }] },
      ],
      delivery: [],
    },
  },
  {
    ...base,
    id: 5,
    name: 'LEGO Icons The Lord of the Rings: Rivendell',
    url: 'https://www.lego.com/en-us/product/the-lord-of-the-rings-rivendell-10316',
    kind: 'generic',
    image_url: IMG.lego,
    status: 'error',
    status_text: 'HTTP 403',
    price: '$499.99',
    last_checked_at: ago(4 * m),
    last_change_at: ago(26 * h),
    last_error: 'HTTP 403 Forbidden — the site blocked the request. Try enabling "Render JavaScript".',
    last_result: { signals: [] },
  },
  {
    ...base,
    id: 6,
    name: 'Sony WH-1000XM6 Wireless Headphones – Black',
    url: 'https://www.amazon.com/dp/B0F3PT1VBL',
    kind: 'generic',
    enabled: false,
    image_url: IMG.headphones,
    status: 'in_stock',
    status_text: 'In Stock',
    price: '$448.00',
    last_checked_at: ago(2 * 24 * h),
    last_change_at: ago(6 * 24 * h),
    generic_config: { mode: 'selector', selector: '#add-to-cart-button', in_stock_text: null, out_of_stock_text: null, render_js: true },
    last_result: { signals: ['selector: #add-to-cart-button found'], matched: 'Add to Cart' },
  },
  {
    ...base,
    id: 7,
    name: 'Mac mini M4 Pro',
    url: 'https://www.apple.com/shop/buy-mac/mac-mini/m4-pro',
    kind: 'apple',
    image_url: null,
    status: 'unknown',
    status_text: 'Waiting for first check',
    price: null,
    notify_enabled: false,
    last_checked_at: null,
    last_change_at: null,
    generic_config: null,
    apple_config: { parts: [{ part_number: 'MCX44LL/A', label: 'M4 Pro 24GB 512GB' }], zip: '95014', max_distance_miles: 25, watch_pickup: true, watch_delivery: true, pickup_today_only: true },
    last_result: null,
  },
  {
    ...base,
    id: 8,
    name: 'Framework Laptop 13 DIY Edition (Ryzen AI 300)',
    url: 'https://frame.work/products/laptop13-diy-amd-ai300',
    kind: 'generic',
    image_url: IMG.framework,
    status: 'out_of_stock',
    status_text: 'Batch 7 sold out',
    price: '$1,049.00',
    interval_minutes: 15,
    last_checked_at: ago(6 * m),
    last_change_at: ago(8 * 24 * h),
    generic_config: { mode: 'text', selector: null, in_stock_text: 'Add to cart', out_of_stock_text: 'sold out', render_js: false },
    last_result: { signals: ['text: found "sold out"'], matched: 'Batch 7 sold out' },
  },
];

export function makeHistory(item: Item): CheckEvent[] {
  const out: CheckEvent[] = [];
  let st: ItemStatus = item.status;
  let t = Date.now() - 30_000;
  for (let i = 0; i < 50; i++) {
    const prev = st;
    // walk backwards: occasionally flip status
    if (i > 0 && Math.random() < 0.08) st = st === 'in_stock' ? 'out_of_stock' : 'in_stock';
    if (i > 0 && Math.random() < 0.04) st = 'error';
    else if (st === 'error' && i > 0) st = prev === 'error' ? 'out_of_stock' : st;
    out.push({
      id: item.id * 1000 + i,
      item_id: item.id,
      checked_at: new Date(t).toISOString(),
      status: i === 0 ? item.status : st,
      status_text: st === 'in_stock' ? 'In stock' : st === 'error' ? null : 'Sold out',
      error: st === 'error' ? 'Timeout after 20s' : null,
      duration_ms: 400 + Math.round(Math.random() * 2400),
      changed: i > 0 && prev !== st,
    });
    t -= item.interval_minutes * 60_000;
  }
  return out;
}

export const notifications: Notification[] = [
  { id: 12, item_id: 1, item_name: 'iPhone 17 Pro 256GB Cosmic Orange', title: 'iPhone 17 Pro is available', message: 'Pickup available at Valley Fair, Stanford; 2-hour delivery available', url: items[0].url, image_url: IMG.iphone, created_at: ago(14 * m), read: false, delivered: true, delivery_error: null },
  { id: 11, item_id: 3, item_name: 'Nintendo Switch 2 – Mario Kart World Bundle', title: 'Nintendo Switch 2 is back in stock', message: 'In stock – ships today · $499.99 at target.com', url: items[2].url, image_url: IMG.switch2, created_at: ago(47 * m), read: false, delivered: false, delivery_error: 'HTTP 429 from ntfy.sh: too many requests' },
  { id: 10, item_id: 4, item_name: 'AirPods Pro 3', title: 'AirPods Pro 3 is available', message: 'Pickup available at Los Gatos', url: items[3].url, image_url: IMG.airpods, created_at: ago(6 * h), read: true, delivered: true, delivery_error: null },
  { id: 9, item_id: 2, item_name: 'PlayStation 5 Pro Console', title: 'PlayStation 5 Pro is back in stock', message: 'Add to cart available at bestbuy.com', url: items[1].url, image_url: IMG.ps5, created_at: ago(3 * 24 * h + 2 * h), read: true, delivered: true, delivery_error: null },
  { id: 8, item_id: null, item_name: null, title: 'Test notification', message: 'If you can read this, ntfy is set up correctly.', url: null, image_url: null, created_at: ago(5 * 24 * h), read: true, delivered: true, delivery_error: null },
  { id: 7, item_id: 6, item_name: 'Sony WH-1000XM6', title: 'Sony WH-1000XM6 is back in stock', message: 'In Stock at amazon.com', url: items[5].url, image_url: IMG.headphones, created_at: ago(6 * 24 * h), read: true, delivered: false, delivery_error: null },
];
