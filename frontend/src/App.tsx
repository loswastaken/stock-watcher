import { QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { lazy, Suspense, useEffect } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Toaster } from 'sonner';
import { AppShell } from './components/layout/AppShell';
import { PageLoader } from './components/PageLoader';
import { ConfirmProvider } from './components/ui/confirm';
import { TooltipProvider } from './components/ui/tooltip';
import { setUnauthorizedHandler } from './lib/api';
import { RequireAdmin, RequireAuth } from './lib/auth';
import { qk, queryClient } from './lib/queryClient';
import { ThemeProvider, useTheme } from './lib/theme';
import type { AuthStatus } from './lib/types';

const Login = lazy(() => import('./pages/Login'));
const Setup = lazy(() => import('./pages/Setup'));
const Dashboard = lazy(() => import('./pages/Dashboard'));
const ItemForm = lazy(() => import('./pages/ItemForm'));
const ItemDetail = lazy(() => import('./pages/ItemDetail'));
const Notifications = lazy(() => import('./pages/Notifications'));
const Purchased = lazy(() => import('./pages/Purchased'));
const Stores = lazy(() => import('./pages/Stores'));
const SettingsPage = lazy(() => import('./pages/Settings'));
const UsersPage = lazy(() => import('./pages/Users'));
const NotFound = lazy(() => import('./pages/NotFound'));

function UnauthorizedBridge() {
  const qc = useQueryClient();
  useEffect(() => {
    setUnauthorizedHandler(() => {
      const cur = qc.getQueryData<AuthStatus>(qk.auth);
      if (cur && !cur.user) return;
      qc.removeQueries({ predicate: (q) => q.queryKey[0] !== 'auth' });
      // RequireAuth sees user=null and redirects to /login?next=...
      qc.setQueryData<AuthStatus>(qk.auth, { setup_required: false, user: null });
    });
    return () => setUnauthorizedHandler(null);
  }, [qc]);
  return null;
}

function ThemedToaster() {
  const { theme } = useTheme();
  return (
    <Toaster
      theme={theme}
      position="top-right"
      closeButton
      richColors
      offset={16}
      mobileOffset={{ top: 'calc(env(safe-area-inset-top) + 8px)' }}
      toastOptions={{ className: 'font-sans' }}
    />
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <TooltipProvider delayDuration={300}>
          <ConfirmProvider>
            <BrowserRouter>
              <UnauthorizedBridge />
              <Suspense fallback={<PageLoader />}>
                <Routes>
                  <Route path="/login" element={<Login />} />
                  <Route path="/setup" element={<Setup />} />
                  <Route element={<RequireAuth />}>
                    <Route element={<AppShell />}>
                      <Route index element={<Dashboard />} />
                      <Route path="items/new" element={<ItemForm />} />
                      <Route path="items/:id" element={<ItemDetail />} />
                      <Route path="items/:id/edit" element={<ItemForm />} />
                      <Route path="notifications" element={<Notifications />} />
                      <Route path="purchased" element={<Purchased />} />
                      <Route path="stores" element={<Stores />} />
                      <Route path="settings" element={<SettingsPage />} />
                      <Route element={<RequireAdmin />}>
                        <Route path="admin/users" element={<UsersPage />} />
                      </Route>
                      <Route path="*" element={<NotFound />} />
                    </Route>
                  </Route>
                </Routes>
              </Suspense>
            </BrowserRouter>
            <ThemedToaster />
          </ConfirmProvider>
        </TooltipProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
