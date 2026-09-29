import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, KeyRound, MoreHorizontal, Shield, ShieldOff, Trash2, UserPlus, Users as UsersIcon } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { toast } from 'sonner';
import { FormError } from '@/components/AuthLayout';
import { Avatar } from '@/components/layout/UserMenu';
import { PageHeader } from '@/components/PageHeader';
import { PasswordInput } from '@/components/PasswordInput';
import { RelativeTime } from '@/components/RelativeTime';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useConfirm } from '@/components/ui/confirm';
import { Dialog, DialogContent } from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { EmptyState } from '@/components/ui/empty-state';
import { Field, Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { SwitchRow } from '@/components/ui/switch';
import { api, errorMessage } from '@/lib/api';
import { useUser } from '@/lib/auth';
import { qk } from '@/lib/queryClient';
import type { User } from '@/lib/types';

export default function UsersPage() {
  const me = useUser();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const users = useQuery({ queryKey: qk.users, queryFn: api.users });
  const [adding, setAdding] = useState(false);
  const [resetting, setResetting] = useState<User | null>(null);

  const update = useMutation({
    mutationFn: ({ id, is_admin }: { id: number; is_admin: boolean }) => api.updateUser(id, { is_admin }),
    onSuccess: (u) => {
      qc.setQueryData<User[]>(qk.users, (l) => l?.map((x) => (x.id === u.id ? u : x)));
      toast.success(u.is_admin ? `${u.username} is now an admin` : `${u.username} is no longer an admin`);
    },
    onError: (e) => toast.error("Couldn't update user", { description: errorMessage(e) }),
  });
  const remove = useMutation({
    mutationFn: (u: User) => api.deleteUser(u.id),
    onSuccess: (_d, u) => {
      qc.setQueryData<User[]>(qk.users, (l) => l?.filter((x) => x.id !== u.id));
      toast.success(`Deleted ${u.username}`);
    },
    onError: (e) => toast.error("Couldn't delete user", { description: errorMessage(e) }),
  });

  const onDelete = async (u: User) => {
    const ok = await confirm({
      title: `Delete ${u.username}?`,
      description: 'Their items, check history and notifications will be permanently deleted.',
      confirmText: 'Delete user',
      destructive: true,
    });
    if (ok) remove.mutate(u);
  };

  const sorted = [...(users.data ?? [])].sort((a, b) => Number(b.is_admin) - Number(a.is_admin) || a.username.localeCompare(b.username));

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader
        title="Users"
        description="Everyone who can sign in to this Stock Watcher. Each user has their own items and notifications."
        actions={
          <Button variant="primary" onClick={() => setAdding(true)}>
            <UserPlus /> Add user
          </Button>
        }
      />

      {users.isError ? (
        <EmptyState icon={<AlertTriangle />} title="Couldn't load users" description={errorMessage(users.error)} action={<Button onClick={() => users.refetch()}>Retry</Button>} />
      ) : (
        <Card className="overflow-hidden">
          <div className="hidden grid-cols-[minmax(0,1fr)_120px_140px_40px] gap-4 border-b border-zinc-100 px-5 py-2.5 text-xs font-medium text-zinc-500 sm:grid dark:border-zinc-800/80 dark:text-zinc-400">
            <span>User</span>
            <span>Role</span>
            <span>Created</span>
            <span />
          </div>
          <ul className="divide-y divide-zinc-100 dark:divide-zinc-800/80">
            {users.isLoading
              ? Array.from({ length: 3 }).map((_, i) => (
                  <li key={i} className="flex items-center gap-3 px-5 py-3.5">
                    <Skeleton className="size-8 rounded-full" />
                    <Skeleton className="h-4 w-32" />
                  </li>
                ))
              : sorted.map((u) => {
                  const self = u.id === me?.id;
                  return (
                    <li key={u.id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-4 px-5 py-3.5 sm:grid-cols-[minmax(0,1fr)_120px_140px_40px]">
                      <div className="flex min-w-0 items-center gap-3">
                        <Avatar name={u.username} className="ring-0" />
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium text-zinc-900 dark:text-zinc-100">
                            {u.username}
                            {self && <span className="ml-2 text-xs font-normal text-zinc-400">(you)</span>}
                          </p>
                          <p className="text-xs text-zinc-500 sm:hidden">
                            {u.is_admin ? 'Admin' : 'Member'} · joined <RelativeTime iso={u.created_at} />
                          </p>
                        </div>
                      </div>
                      <div className="hidden sm:block">
                        {u.is_admin ? (
                          <Badge tone="indigo">
                            <Shield /> Admin
                          </Badge>
                        ) : (
                          <Badge>Member</Badge>
                        )}
                      </div>
                      <div className="hidden text-sm text-zinc-500 sm:block dark:text-zinc-400">
                        <RelativeTime iso={u.created_at} />
                      </div>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <button
                            type="button"
                            aria-label={`Actions for ${u.username}`}
                            className="flex size-8 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 data-[state=open]:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100 dark:data-[state=open]:bg-zinc-800"
                          >
                            <MoreHorizontal className="size-4" />
                          </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent>
                          <DropdownMenuItem onSelect={() => setResetting(u)}>
                            <KeyRound /> Reset password
                          </DropdownMenuItem>
                          <DropdownMenuItem onSelect={() => update.mutate({ id: u.id, is_admin: !u.is_admin })}>
                            {u.is_admin ? (
                              <>
                                <ShieldOff /> Remove admin
                              </>
                            ) : (
                              <>
                                <Shield /> Make admin
                              </>
                            )}
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem destructive disabled={self} onSelect={() => onDelete(u)}>
                            <Trash2 /> {self ? "Can't delete yourself" : 'Delete user'}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </li>
                  );
                })}
          </ul>
          {!users.isLoading && !sorted.length && (
            <EmptyState className="m-4" icon={<UsersIcon />} title="No users" />
          )}
        </Card>
      )}

      <AddUserDialog open={adding} onOpenChange={setAdding} />
      <ResetPasswordDialog user={resetting} onClose={() => setResetting(null)} />
    </div>
  );
}

function AddUserDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [isAdmin, setIsAdmin] = useState(false);
  const [touched, setTouched] = useState(false);
  const create = useMutation({
    mutationFn: () => api.createUser({ username: username.trim(), password, is_admin: isAdmin }),
    onSuccess: (u) => {
      qc.setQueryData<User[]>(qk.users, (l) => (l ? [...l, u] : [u]));
      toast.success(`Created ${u.username}`);
      close(false);
    },
  });
  const close = (o: boolean) => {
    onOpenChange(o);
    if (!o) {
      setUsername('');
      setPassword('');
      setIsAdmin(false);
      setTouched(false);
      create.reset();
    }
  };
  const pwErr = password.length < 8 ? 'At least 8 characters' : undefined;
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!username.trim() || pwErr) return;
    create.mutate();
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent title="Add user" description="They can sign in right away with these credentials.">
        <form id="add-user" onSubmit={onSubmit} className="space-y-4 pb-1" noValidate>
          <FormError>{create.error ? errorMessage(create.error) : null}</FormError>
          <Field label="Username" htmlFor="nu-name" error={touched && !username.trim() ? 'Required' : undefined}>
            <Input id="nu-name" autoComplete="off" autoCapitalize="none" spellCheck={false} value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
          </Field>
          <Field label="Password" htmlFor="nu-pw" error={touched ? pwErr : undefined} hint="At least 8 characters.">
            <PasswordInput id="nu-pw" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} invalid={touched && !!pwErr} />
          </Field>
          <SwitchRow id="nu-admin" icon={<Shield />} title="Administrator" description="Can manage users." checked={isAdmin} onCheckedChange={setIsAdmin} />
        </form>
        <div className="flex flex-col-reverse gap-2 pb-2 pt-4 sm:flex-row sm:justify-end">
          <Button variant="ghost" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="submit" form="add-user" variant="primary" loading={create.isPending}>
            Create user
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ResetPasswordDialog({ user, onClose }: { user: User | null; onClose: () => void }) {
  const [password, setPassword] = useState('');
  const [touched, setTouched] = useState(false);
  const reset = useMutation({
    mutationFn: () => api.updateUser(user!.id, { password }),
    onSuccess: (u) => {
      toast.success(`Password reset for ${u.username}`);
      close();
    },
  });
  const close = () => {
    setPassword('');
    setTouched(false);
    reset.reset();
    onClose();
  };
  const pwErr = password.length < 8 ? 'At least 8 characters' : undefined;
  return (
    <Dialog open={!!user} onOpenChange={(o) => !o && close()}>
      <DialogContent title={`Reset password`} description={user ? `Set a new password for ${user.username}.` : undefined}>
        <form
          id="reset-pw"
          onSubmit={(e) => {
            e.preventDefault();
            setTouched(true);
            if (!pwErr) reset.mutate();
          }}
          className="space-y-4 pb-1"
          noValidate
        >
          <FormError>{reset.error ? errorMessage(reset.error) : null}</FormError>
          <Field label="New password" htmlFor="rp-pw" error={touched ? pwErr : undefined} hint="At least 8 characters.">
            <PasswordInput id="rp-pw" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus invalid={touched && !!pwErr} />
          </Field>
        </form>
        <div className="flex flex-col-reverse gap-2 pb-2 pt-4 sm:flex-row sm:justify-end">
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" form="reset-pw" variant="primary" loading={reset.isPending}>
            Reset password
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
