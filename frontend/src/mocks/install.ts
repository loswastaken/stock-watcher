/* Dev-only fetch mock (VITE_MOCK=1). Never imported in production builds. */
import type { AuthStatus, Item, ItemCreate, Notification, Stats, User } from '@/lib/types';
import { ago, items, makeHistory, notifications, settings, users } from './data';
import { IMG } from './images';

const store = {
  get authed() {
    return localStorage.getItem('mock-authed') !== '0';
  },
  set authed(v: boolean) {
    localStorage.setItem('mock-authed', v ? '1' : '0');
  },
  get setup() {
    return localStorage.getItem('mock-setup') === '1';
  },
};
const me: User = users[0];
let nextId = 100;

const json = (data: unknown, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

function stats(): Stats {
  return {
    total: items.length,
    in_stock: items.filter((i) => i.status === 'in_stock').length,
    out_of_stock: items.filter((i) => i.status === 'out_of_stock').length,
    unknown: items.filter((i) => i.status === 'unknown').length,
    error: items.filter((i) => i.status === 'error').length,
    paused: items.filter((i) => !i.enabled).length,
    unread_notifications: notifications.filter((n) => !n.read).length,
    checks_24h: 4213,
    alerts_24h: notifications.filter((n) => Date.now() - Date.parse(n.created_at) < 86_400_000).length,
  };
}

async function handle(path: string, method: string, body: unknown): Promise<Response> {
  const url = new URL(path, location.origin);
  const p = url.pathname.replace(/^\/api/, '');
  await delay(p.includes('preview') || p.includes('resolve') || p.endsWith('/check') ? 900 : 150);

  if (p === '/auth/status')
    return json({ setup_required: store.setup, user: store.authed && !store.setup ? me : null } satisfies AuthStatus);
  if (p === '/auth/login') {
    const b = body as { username: string; password: string };
    if (b.password === 'ratelimit') return json({ detail: 'Too many login attempts' }, 429);
    if (b.password.length < 3) return json({ detail: 'Invalid username or password' }, 401);
    store.authed = true;
    return json(me);
  }
  if (p === '/auth/setup') {
    localStorage.removeItem('mock-setup');
    store.authed = true;
    return json(me);
  }
  if (p === '/auth/logout') {
    store.authed = false;
    return json(null, 204);
  }
  if (!store.authed) return json({ detail: 'Not authenticated' }, 401);
  if (p === '/auth/me') return json(me);
  if (p === '/auth/change-password') {
    const b = body as { current_password: string };
    return b.current_password === 'wrong' ? json({ detail: 'Current password is incorrect' }, 400) : json(null, 204);
  }

  if (p === '/users' && method === 'GET') return json(users);
  if (p === '/users' && method === 'POST') {
    const b = body as { username: string; is_admin: boolean };
    if (users.some((u) => u.username.toLowerCase() === b.username.toLowerCase())) return json({ detail: 'Username already exists' }, 409);
    const u = { id: nextId++, username: b.username, is_admin: b.is_admin, created_at: ago(0) };
    users.push(u);
    return json(u);
  }
  let mm = p.match(/^\/users\/(\d+)$/);
  if (mm) {
    const u = users.find((x) => x.id === Number(mm![1]));
    if (!u) return json({ detail: 'Not found' }, 404);
    if (method === 'DELETE') {
      users.splice(users.indexOf(u), 1);
      return json(null, 204);
    }
    const b = body as { is_admin?: boolean };
    if (b.is_admin === false && users.filter((x) => x.is_admin).length === 1 && u.is_admin)
      return json({ detail: 'Cannot remove the last admin' }, 400);
    if (b.is_admin !== undefined) u.is_admin = b.is_admin;
    return json(u);
  }

  if (p === '/settings' && method === 'GET') return json(settings);
  if (p === '/settings') {
    const b = body as Record<string, unknown>;
    Object.assign(settings, b);
    if ('ntfy_token' in b) settings.ntfy_token_set = !!b.ntfy_token;
    delete (settings as Record<string, unknown>).ntfy_token;
    return json(settings);
  }
  if (p === '/settings/test-notification')
    return json(settings.ntfy_topic ? { ok: true, error: null } : { ok: false, error: 'No topic configured' });

  if (p === '/stats') return json(stats());

  if (p === '/items/preview') {
    const u = (body as { url: string }).url;
    const apple = /apple\.com/.test(u);
    return json({
      name: apple ? 'iPhone 17 Pro' : 'Steam Deck OLED 1TB',
      image_url: apple ? IMG.iphone : IMG.switch2,
      price: apple ? '$1,099.00' : '$649.00',
      status: apple ? 'unknown' : 'out_of_stock',
      is_apple: apple,
    });
  }
  if (p === '/apple/resolve')
    return json({
      product_name: 'iPhone 17 Pro',
      image_url: IMG.iphone,
      variants: [
        { part_number: 'MG8H4LL/A', label: '256GB Cosmic Orange', price: '$1,099.00' },
        { part_number: 'MG8J4LL/A', label: '512GB Cosmic Orange', price: '$1,299.00' },
        { part_number: 'MG8K4LL/A', label: '1TB Cosmic Orange', price: '$1,499.00' },
        { part_number: 'MG8L4LL/A', label: '256GB Deep Blue', price: '$1,099.00' },
        { part_number: 'MG8M4LL/A', label: '512GB Deep Blue', price: '$1,299.00' },
        { part_number: 'MG8N4LL/A', label: '256GB Silver', price: '$1,099.00' },
      ],
    });

  if (p === '/items' && method === 'GET') return json(items);
  if (p === '/items' && method === 'POST') {
    const b = body as ItemCreate;
    const it: Item = {
      id: nextId++,
      name: b.name || 'New item',
      url: b.url,
      kind: b.kind ?? 'generic',
      enabled: true,
      notify_enabled: b.notify_enabled ?? true,
      interval_minutes: b.interval_minutes ?? 2,
      image_url: null,
      status: 'unknown',
      status_text: null,
      price: null,
      last_checked_at: null,
      last_change_at: null,
      last_error: null,
      generic_config: b.generic_config ?? null,
      apple_config: b.apple_config ?? null,
      last_result: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    items.push(it);
    setTimeout(() => {
      it.image_url = b.image_url ?? null;
      it.last_checked_at = new Date().toISOString();
      it.status = 'out_of_stock';
      it.status_text = 'Sold out';
      it.price = '$649.00';
    }, 4000);
    return json(it, 201);
  }
  mm = p.match(/^\/items\/(\d+)(\/.*)?$/);
  if (mm) {
    const it = items.find((x) => x.id === Number(mm![1]));
    if (!it) return json({ detail: 'Item not found' }, 404);
    const sub = mm[2] ?? '';
    if (sub === '' && method === 'GET') return json(it);
    if (sub === '' && method === 'PATCH') {
      Object.assign(it, body, { updated_at: new Date().toISOString() });
      return json(it);
    }
    if (sub === '' && method === 'DELETE') {
      items.splice(items.indexOf(it), 1);
      return json(null, 204);
    }
    if (sub === '/check') {
      it.last_checked_at = new Date().toISOString();
      return json(it);
    }
    if (sub === '/image/refresh') return it.image_url ? json(it) : json({ detail: 'No image found on the page' }, 422);
    if (sub === '/image') return json(it);
    if (sub.startsWith('/history')) return json(makeHistory(it));
  }

  if (p === '/notifications' && method === 'GET') {
    const unread = url.searchParams.get('unread_only') === 'true';
    const limit = Number(url.searchParams.get('limit') ?? 50);
    const offset = Number(url.searchParams.get('offset') ?? 0);
    const list = notifications.filter((n) => !unread || !n.read);
    return json({ items: list.slice(offset, offset + limit), unread_count: notifications.filter((n) => !n.read).length, total: list.length });
  }
  if (p === '/notifications' && method === 'DELETE') {
    notifications.splice(0);
    return json(null, 204);
  }
  if (p === '/notifications/read-all') {
    notifications.forEach((n) => (n.read = true));
    return json(null, 204);
  }
  mm = p.match(/^\/notifications\/(\d+)(\/read)?$/);
  if (mm) {
    const i = notifications.findIndex((n) => n.id === Number(mm![1]));
    if (i === -1) return json({ detail: 'Not found' }, 404);
    if (mm[2]) notifications[i].read = true;
    else notifications.splice(i, 1);
    return json(null, 204);
  }
  return json({ detail: `Mock: no handler for ${method} ${p}` }, 404);
}

export function installMocks() {
  const real = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (!url.startsWith('/api/') && !url.startsWith(`${location.origin}/api/`)) return real(input, init);
    const method = (init?.method ?? 'GET').toUpperCase();
    let body: unknown = null;
    if (typeof init?.body === 'string') body = JSON.parse(init.body);
    return handle(url, method, body);
  };
  // Handy for testing the "new alert" toast from devtools / Playwright.
  (window as unknown as { __mockAlert: () => void }).__mockAlert = () => {
    const n: Notification = {
      id: nextId++,
      item_id: 2,
      item_name: 'PlayStation 5 Pro Console',
      title: 'PlayStation 5 Pro is back in stock',
      message: 'Add to cart available at bestbuy.com · $699.99',
      url: items[1].url,
      image_url: IMG.ps5,
      created_at: new Date().toISOString(),
      read: false,
      delivered: true,
      delivery_error: null,
    };
    notifications.unshift(n);
  };
  console.info('[mock] API mocks installed');
}
