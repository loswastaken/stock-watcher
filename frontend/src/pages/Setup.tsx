import { useMutation } from '@tanstack/react-query';
import { Sparkles, User as UserIcon } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { AuthLayout, FormError } from '@/components/AuthLayout';
import { FullScreenLoader } from '@/components/FullScreenLoader';
import { PasswordInput } from '@/components/PasswordInput';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/input';
import { api, errorMessage } from '@/lib/api';
import { useAuthStatus, useSetAuth } from '@/lib/auth';

export default function Setup() {
  const { data, isLoading, isError, refetch } = useAuthStatus();
  const navigate = useNavigate();
  const setAuth = useSetAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [touched, setTouched] = useState(false);

  const setup = useMutation({
    mutationFn: () => api.setup(username.trim(), password),
    onSuccess: (user) => {
      setAuth(user);
      navigate('/', { replace: true });
    },
  });

  if (isLoading) return <FullScreenLoader />;
  if (isError) return <FullScreenLoader error onRetry={() => refetch()} />;
  if (data && !data.setup_required && !setup.isPending) return <Navigate to={data.user ? '/' : '/login'} replace />;

  const pwShort = password.length > 0 && password.length < 8;
  const mismatch = confirm.length > 0 && confirm !== password;
  const valid = username.trim().length > 0 && password.length >= 8 && confirm === password;

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (valid) setup.mutate();
  };

  return (
    <AuthLayout
      title="Welcome to Stock Watcher"
      subtitle="Create the administrator account to get started."
      footer="Only admins can create additional accounts later."
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <FormError>{setup.error ? errorMessage(setup.error) : null}</FormError>
        <Field label="Username" htmlFor="username" error={touched && !username.trim() ? 'Username is required' : undefined}>
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
          />
        </Field>
        <Field label="Password" htmlFor="password" hint="At least 8 characters." error={pwShort && touched ? 'Password must be at least 8 characters' : undefined}>
          <PasswordInput
            id="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            invalid={pwShort && touched}
            className="h-10"
          />
        </Field>
        <Field label="Confirm password" htmlFor="confirm" error={mismatch ? "Passwords don't match" : undefined}>
          <PasswordInput
            id="confirm"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            invalid={mismatch}
            className="h-10"
          />
        </Field>
        <Button type="submit" variant="primary" size="lg" className="w-full" loading={setup.isPending}>
          {!setup.isPending && <Sparkles />} Create admin account
        </Button>
      </form>
    </AuthLayout>
  );
}
