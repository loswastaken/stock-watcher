import { QueryClient } from '@tanstack/react-query';
import { ApiError } from './api';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: true,
      retry: (count, err) => {
        if (err instanceof ApiError && err.status >= 400 && err.status < 500) return false;
        return count < 2;
      },
    },
    mutations: { retry: false },
  },
});

export const qk = {
  auth: ['auth'] as const,
  items: ['items'] as const,
  item: (id: number) => ['items', id] as const,
  history: (id: number) => ['history', id] as const,
  stores: (id: number) => ['stores', id] as const,
  restocks: (id: number) => ['restocks', id] as const,
  retailers: ['retailers'] as const,
  stats: ['stats'] as const,
  settings: ['settings'] as const,
  notifications: ['notifications'] as const,
  unread: ['notifications', 'unread-peek'] as const,
  users: ['users'] as const,
};
