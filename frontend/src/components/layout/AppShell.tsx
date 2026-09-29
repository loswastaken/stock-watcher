import { useQuery } from '@tanstack/react-query';
import { Bell, LayoutGrid, Plus, Settings, Users } from 'lucide-react';
import { Suspense, useEffect, useRef } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { api } from '@/lib/api';
import { useUser } from '@/lib/auth';
import { qk } from '@/lib/queryClient';
import { useTheme } from '@/lib/theme';
import { cn } from '@/lib/utils';
import { Logo } from '../Logo';
import { NotificationBell, UnreadBadge } from '../notifications/NotificationBell';
import { useUnread, useUnreadWatcher } from '../notifications/useUnread';
import { buttonClasses } from '../ui/button';
import { PageLoader } from '../PageLoader';
import { UserMenu } from './UserMenu';

interface NavItem {
  to: string;
  label: string;
  icon: typeof LayoutGrid;
  end?: boolean;
  admin?: boolean;
  badge?: boolean;
}

const NAV: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutGrid, end: true },
  { to: '/notifications', label: 'Notifications', icon: Bell, badge: true },
  { to: '/settings', label: 'Settings', icon: Settings },
  { to: '/admin/users', label: 'Users', icon: Users, admin: true },
];

function useThemeSync() {
  // Adopt the server-side theme preference once per session.
  const { data } = useQuery({ queryKey: qk.settings, queryFn: api.settings, staleTime: 60_000 });
  const { theme, setTheme } = useTheme();
  const done = useRef(false);
  useEffect(() => {
    if (!data || done.current) return;
    done.current = true;
    if (data.theme && data.theme !== theme) setTheme(data.theme);
  }, [data, theme, setTheme]);
}

export function AppShell() {
  const user = useUser();
  const { data: unread } = useUnread();
  const unreadCount = unread?.unread_count ?? 0;
  const location = useLocation();
  useUnreadWatcher();
  useThemeSync();

  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [location.pathname]);

  const nav = NAV.filter((n) => !n.admin || user?.is_admin);

  return (
    <div className="min-h-dvh bg-zinc-50 dark:bg-zinc-950">
      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 flex-col border-r border-zinc-200 bg-white/70 backdrop-blur-xl lg:flex dark:border-zinc-800/80 dark:bg-zinc-950/70">
        <Link to="/" className="flex h-16 items-center gap-2.5 px-5 focus-visible:outline-none">
          <Logo className="size-7" />
          <span className="text-[15px] font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">Stock Watcher</span>
        </Link>
        <div className="px-3 pb-3">
          <Link to="/items/new" className={buttonClasses({ variant: 'primary', className: 'w-full' })}>
            <Plus /> Add item
          </Link>
        </div>
        <nav className="flex-1 space-y-0.5 px-3" aria-label="Main">
          {nav.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                cn(
                  'group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60',
                  isActive
                    ? 'bg-zinc-100 text-zinc-900 dark:bg-zinc-800/80 dark:text-zinc-50'
                    : 'text-zinc-600 hover:bg-zinc-100/70 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800/40 dark:hover:text-zinc-100',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <n.icon
                    className={cn(
                      'size-4 transition-colors',
                      isActive ? 'text-indigo-600 dark:text-indigo-400' : 'text-zinc-400 group-hover:text-zinc-600 dark:group-hover:text-zinc-300',
                    )}
                  />
                  <span className="flex-1">{n.label}</span>
                  {n.badge && unreadCount > 0 && (
                    <span className="rounded-full bg-indigo-100 px-1.5 py-0.5 text-[11px] font-semibold tabular-nums text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300">
                      {unreadCount}
                    </span>
                  )}
                </>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-zinc-200 p-3 dark:border-zinc-800/80">
          <UserMenu variant="full" />
        </div>
      </aside>

      {/* Top bar */}
      <header className="sticky top-0 z-20 border-b border-zinc-200/80 bg-white/75 pt-[env(safe-area-inset-top)] backdrop-blur-xl lg:ml-60 dark:border-zinc-800/80 dark:bg-zinc-950/75">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-3 px-4 sm:px-6 lg:h-16 lg:px-8">
          <Link to="/" className="flex items-center gap-2 lg:hidden">
            <Logo className="size-7" />
            <span className="text-[15px] font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">Stock Watcher</span>
          </Link>
          <div className="flex-1" />
          <NotificationBell />
          <div className="lg:hidden">
            <UserMenu />
          </div>
        </div>
      </header>

      <main className="pb-[calc(5rem+env(safe-area-inset-bottom))] lg:ml-60 lg:pb-10">
        <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
          <Suspense fallback={<PageLoader />}>
            <Outlet />
          </Suspense>
        </div>
      </main>

      {/* Mobile tab bar */}
      <nav
        aria-label="Main"
        className="fixed inset-x-0 bottom-0 z-30 border-t border-zinc-200 bg-white/85 pb-[env(safe-area-inset-bottom)] backdrop-blur-xl lg:hidden dark:border-zinc-800 dark:bg-zinc-950/85"
      >
        <div className="mx-auto flex h-16 max-w-md items-stretch justify-around px-2">
          {nav.slice(0, 2).map((n) => (
            <TabLink key={n.to} item={n} badge={n.badge ? unreadCount : 0} />
          ))}
          <div className="flex flex-1 items-center justify-center">
            <Link
              to="/items/new"
              aria-label="Add item"
              className="flex size-12 items-center justify-center rounded-2xl bg-indigo-600 text-white shadow-lg shadow-indigo-600/30 transition-transform active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 focus-visible:ring-offset-2 dark:bg-indigo-500 dark:focus-visible:ring-offset-zinc-950"
            >
              <Plus className="size-6" />
            </Link>
          </div>
          {nav.slice(2).map((n) => (
            <TabLink key={n.to} item={n} badge={0} />
          ))}
        </div>
      </nav>
    </div>
  );
}

function TabLink({ item, badge }: { item: NavItem; badge: number }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
      className={({ isActive }) =>
        cn(
          'relative flex flex-1 flex-col items-center justify-center gap-1 text-[11px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-indigo-500/60',
          isActive ? 'text-indigo-600 dark:text-indigo-400' : 'text-zinc-500 dark:text-zinc-400',
        )
      }
    >
      <span className="relative">
        <item.icon className="size-5" />
        <UnreadBadge count={badge} className="absolute -right-2.5 -top-1.5" />
      </span>
      {item.label === 'Notifications' ? 'Alerts' : item.label}
    </NavLink>
  );
}
