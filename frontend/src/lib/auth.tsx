import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { api } from './api';
import { qk } from './queryClient';
import type { AuthStatus, User } from './types';
import { FullScreenLoader } from '@/components/FullScreenLoader';

export function useAuthStatus() {
  return useQuery({ queryKey: qk.auth, queryFn: api.authStatus, staleTime: 5 * 60_000, retry: 1 });
}

export function useUser(): User | null {
  const { data } = useAuthStatus();
  return data?.user ?? null;
}

/** Update cached auth state after login/setup/logout. */
export function useSetAuth() {
  const qc = useQueryClient();
  return useCallback(
    (user: User | null) => {
      if (!user) {
        // Drop everything user-scoped.
        qc.removeQueries({ predicate: (q) => q.queryKey[0] !== 'auth' });
      }
      qc.setQueryData<AuthStatus>(qk.auth, { setup_required: false, user });
    },
    [qc],
  );
}

export function RequireAuth() {
  const { data, isLoading, isError, refetch } = useAuthStatus();
  const location = useLocation();
  if (isLoading) return <FullScreenLoader />;
  if (isError || !data) return <FullScreenLoader error onRetry={() => refetch()} />;
  if (data.setup_required) return <Navigate to="/setup" replace />;
  if (!data.user) {
    const next = location.pathname + location.search;
    return <Navigate to={next && next !== '/' ? `/login?next=${encodeURIComponent(next)}` : '/login'} replace />;
  }
  return <Outlet />;
}

export function RequireAdmin() {
  const user = useUser();
  if (!user?.is_admin) return <Navigate to="/" replace />;
  return <Outlet />;
}

export function safeNext(next: string | null): string {
  if (!next || !next.startsWith('/') || next.startsWith('//')) return '/';
  if (next.startsWith('/login') || next.startsWith('/setup')) return '/';
  return next;
}
