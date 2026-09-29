import { useMutation } from '@tanstack/react-query';
import { Check, LogOut, Monitor, Moon, Settings, Shield, Sun, Users } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { useSetAuth, useUser } from '@/lib/auth';
import { useTheme } from '@/lib/theme';
import type { Theme } from '@/lib/types';
import { cn } from '@/lib/utils';
import { useSaveTheme } from '@/hooks/useSaveTheme';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '../ui/dropdown-menu';

export function Avatar({ name, className }: { name: string; className?: string }) {
  return (
    <span
      className={cn(
        'flex size-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-indigo-400 to-violet-600 text-xs font-semibold uppercase text-white ring-2 ring-white dark:ring-zinc-900',
        className,
      )}
    >
      {name.slice(0, 2)}
    </span>
  );
}

const themeOpts: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
];

export function UserMenu({ variant = 'compact' }: { variant?: 'compact' | 'full' }) {
  const user = useUser();
  const setAuth = useSetAuth();
  const navigate = useNavigate();
  const { theme } = useTheme();
  const saveTheme = useSaveTheme();
  const logout = useMutation({
    mutationFn: api.logout,
    onSettled: (_d, err) => {
      if (err) toast.error(errorMessage(err));
      setAuth(null);
      navigate('/login', { replace: true });
    },
  });
  if (!user) return null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {variant === 'full' ? (
          <button
            type="button"
            className="flex w-full items-center gap-3 rounded-lg p-2 text-left transition-colors hover:bg-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 data-[state=open]:bg-zinc-100 dark:hover:bg-zinc-800/60 dark:data-[state=open]:bg-zinc-800/60"
          >
            <Avatar name={user.username} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-zinc-900 dark:text-zinc-100">{user.username}</span>
              <span className="block text-xs text-zinc-500">{user.is_admin ? 'Administrator' : 'Member'}</span>
            </span>
          </button>
        ) : (
          <button
            type="button"
            className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-zinc-950"
            aria-label="Account menu"
          >
            <Avatar name={user.username} />
          </button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-56" align={variant === 'full' ? 'start' : 'end'} side={variant === 'full' ? 'top' : 'bottom'}>
        <DropdownMenuLabel className="flex items-center gap-2 text-zinc-900 dark:text-zinc-100">
          <span className="truncate text-sm font-medium">{user.username}</span>
          {user.is_admin && (
            <span className="inline-flex items-center gap-0.5 rounded bg-indigo-100 px-1 py-0.5 text-[10px] font-medium text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300">
              <Shield className="size-2.5" /> Admin
            </span>
          )}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => navigate('/settings')}>
          <Settings /> Settings
        </DropdownMenuItem>
        {user.is_admin && (
          <DropdownMenuItem onSelect={() => navigate('/admin/users')}>
            <Users /> Users
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        {themeOpts.map((t) => (
          <DropdownMenuItem key={t.value} onSelect={(e) => { e.preventDefault(); saveTheme(t.value); }}>
            <t.icon /> {t.label}
            {theme === t.value && <Check className="ml-auto !text-indigo-500" />}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => logout.mutate()}>
          <LogOut /> Log out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
