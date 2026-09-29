import type {
  AppleResolve,
  UpdateStatus,
  AuthStatus,
  CheckEvent,
  Item,
  ItemCreate,
  ItemUpdate,
  NotificationList,
  Preview,
  Restock,
  Retailer,
  RetailerConfig,
  Settings,
  StoreRow,
  SettingsUpdate,
  Stats,
  TestNotificationResult,
  User,
} from './types';

type ValidationIssue = { loc?: (string | number)[]; msg?: string; type?: string };

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(formatDetail(detail, status));
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** Render FastAPI `detail` (string or validation array) as human text. */
export function formatDetail(detail: unknown, status?: number): string {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (status === 413) return 'File too large (max 8 MB).';
  if (Array.isArray(detail)) {
    const parts = (detail as ValidationIssue[]).map((d) => {
      const loc = (d.loc ?? []).filter((p) => p !== 'body' && p !== 'query' && p !== 'path');
      const field = loc.length ? `${loc.join('.')}: ` : '';
      return `${field}${d.msg ?? 'invalid value'}`;
    });
    if (parts.length) return parts.join('; ');
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message);
  }
  if (status === 429) return 'Too many attempts. Please wait and try again.';
  if (status && status >= 500) return 'Server error. Please try again.';
  if (status === 0) return 'Network error. Is the server reachable?';
  return status ? `Request failed (${status})` : 'Request failed';
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;
export function setUnauthorizedHandler(fn: UnauthorizedHandler | null) {
  onUnauthorized = fn;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  form?: FormData;
  /** Don't trigger the global 401 redirect (e.g. auth/status, login). */
  skipAuthRedirect?: boolean;
  signal?: AbortSignal;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  let body: BodyInit | undefined;
  if (opts.form) {
    body = opts.form;
  } else if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  } else if (opts.method && opts.method !== 'GET') {
    // CSRF guard on the backend expects JSON content type on mutating requests.
    headers['Content-Type'] = 'application/json';
  }

  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method: opts.method ?? 'GET',
      headers,
      body,
      credentials: 'same-origin',
      signal: opts.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, null);
  }

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? (data as { detail: unknown }).detail : data;
    if (res.status === 401 && !opts.skipAuthRedirect) onUnauthorized?.();
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

const q = (params: Record<string, string | number | boolean | undefined>) => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) sp.set(k, String(v));
  const s = sp.toString();
  return s ? `?${s}` : '';
};

export const api = {
  // auth
  authStatus: () => request<AuthStatus>('/auth/status', { skipAuthRedirect: true }),
  setup: (username: string, password: string) =>
    request<User>('/auth/setup', { method: 'POST', body: { username, password }, skipAuthRedirect: true }),
  login: (username: string, password: string) =>
    request<User>('/auth/login', { method: 'POST', body: { username, password }, skipAuthRedirect: true }),
  logout: () => request<void>('/auth/logout', { method: 'POST', skipAuthRedirect: true }),
  me: () => request<User>('/auth/me'),
  changePassword: (current_password: string, new_password: string) =>
    request<void>('/auth/change-password', { method: 'POST', body: { current_password, new_password } }),

  // users
  users: () => request<User[]>('/users'),
  createUser: (body: { username: string; password: string; is_admin: boolean }) =>
    request<User>('/users', { method: 'POST', body }),
  updateUser: (id: number, body: { password?: string; is_admin?: boolean }) =>
    request<User>(`/users/${id}`, { method: 'PATCH', body }),
  deleteUser: (id: number) => request<void>(`/users/${id}`, { method: 'DELETE' }),

  // settings
  settings: () => request<Settings>('/settings'),
  updateSettings: (body: SettingsUpdate) => request<Settings>('/settings', { method: 'PUT', body }),
  testNotification: () => request<TestNotificationResult>('/settings/test-notification', { method: 'POST' }),

  // items
  items: () => request<Item[]>('/items'),
  item: (id: number) => request<Item>(`/items/${id}`),
  createItem: (body: ItemCreate) => request<Item>('/items', { method: 'POST', body }),
  updateItem: (id: number, body: ItemUpdate) => request<Item>(`/items/${id}`, { method: 'PATCH', body }),
  deleteItem: (id: number) => request<void>(`/items/${id}`, { method: 'DELETE' }),
  purchaseItem: (id: number) => request<Item>(`/items/${id}/purchase`, { method: 'POST' }),
  unpurchaseItem: (id: number) => request<Item>(`/items/${id}/unpurchase`, { method: 'POST' }),
  checkItem: (id: number) => request<Item>(`/items/${id}/check`, { method: 'POST' }),
  health: () => request<{ status: string; version: string }>('/health'),
  updateStatus: () => request<UpdateStatus>('/system/update'),
  updateCheck: () => request<UpdateStatus>('/system/update/check', { method: 'POST' }),
  updateApply: () => request<{ started: boolean }>('/system/update/apply', { method: 'POST' }),
  checkAll: () => request<{ queued: number; total: number }>('/items/check-all', { method: 'POST' }),
  uploadImage: (id: number, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<Item>(`/items/${id}/image`, { method: 'POST', form });
  },
  refreshImage: (id: number) => request<Item>(`/items/${id}/image/refresh`, { method: 'POST' }),
  stores: (id: number) => request<StoreRow[]>(`/items/${id}/stores`),
  addStore: (id: number, url: string, retailer_config?: RetailerConfig) =>
    request<Item>(`/items/${id}/stores`, { method: 'POST', body: { url, ...(retailer_config ? { retailer_config } : {}) } }),
  restocks: (id: number, limit = 20) => request<Restock[]>(`/items/${id}/restocks${q({ limit })}`),
  retailers: () => request<Retailer[]>('/retailers'),
  history: (id: number, limit = 50) => request<CheckEvent[]>(`/items/${id}/history${q({ limit })}`),
  preview: (url: string, signal?: AbortSignal) =>
    request<Preview>('/items/preview', { method: 'POST', body: { url }, signal }),

  // apple
  appleResolve: (url: string, signal?: AbortSignal) =>
    request<AppleResolve>('/apple/resolve', { method: 'POST', body: { url }, signal }),

  // notifications
  notifications: (p: { unread_only?: boolean; limit?: number; offset?: number } = {}) =>
    request<NotificationList>(`/notifications${q(p)}`),
  markRead: (id: number) => request<void>(`/notifications/${id}/read`, { method: 'POST' }),
  markAllRead: () => request<void>('/notifications/read-all', { method: 'POST' }),
  deleteNotification: (id: number) => request<void>(`/notifications/${id}`, { method: 'DELETE' }),
  clearNotifications: () => request<void>('/notifications', { method: 'DELETE' }),

  // stats
  stats: () => request<Stats>('/stats'),
};
