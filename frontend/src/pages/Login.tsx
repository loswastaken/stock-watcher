import { useMutation } from '@tanstack/react-query';
import { LogIn, User as UserIcon } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom';
import { AuthLayout, FormError } from '@/components/AuthLayout';
import { FullScreenLoader } from '@/components/FullScreenLoader';
import { PasswordInput } from '@/components/PasswordInput';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/input';
import { api, ApiError, errorMessage } from '@/lib/api';
import { safeNext, useAuthStatus, useSetAuth } from '@/lib/auth';

export default function Login() {
  const { data, isLoading, isError, refetch } = useAuthStatus();
  const [params] = useSearchParams();
  const next = safeNext(params.get('next'));
  const navigate = useNavigate();
  const setAuth = useSetAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  const login = useMutation({
    mutationFn: () => api.login(username.trim(), password),
    onSuccess: (user) => {
      setAuth(user);
      navigate(next, { replace: true });
    },
  });

  if (isLoading) return <FullScreenLoader />;
  if (isError) return <FullScreenLoader error onRetry={() => refetch()} />;
  if (data?.setup_required) return <Navigate to="/setup" replace />;
  if (data?.user && !login.isPending) return <Navigate to={next} replace />;

  const err = login.error;
  const message =
    err instanceof ApiError && err.status === 429
      ? 'Too many login attempts. Please wait a few minutes and try again.'
      : err instanceof ApiError && err.status === 401
        ? 'Incorrect username or password.'
        : err
          ? errorMessage(err)
          : null;

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) return;
    login.mutate();
  };

  return (
    <AuthLayout title="Sign in to Stock Watcher" subtitle="Restock alerts for the things you want.">
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormError>{message}</FormError>
        <Field label="Username" htmlFor="username">
          <Input
            id="username"
            autoComplete="username"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            leading={<UserIcon />}
            className="h-10"
            required
          />
        </Field>
        <Field label="Password" htmlFor="password">
          <PasswordInput
            id="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="h-10"
            required
          />
        </Field>
        <Button type="submit" variant="primary" size="lg" className="w-full" loading={login.isPending} disabled={!username.trim() || !password}>
          {!login.isPending && <LogIn />} Sign in
        </Button>
      </form>
    </AuthLayout>
  );
}
