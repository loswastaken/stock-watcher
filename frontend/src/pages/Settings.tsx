import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  Dices,
  KeyRound,
  Lock,
  MapPin,
  Monitor,
  Moon,
  Palette,
  Send,
  Server,
  SlidersHorizontal,
  Smartphone,
  Sun,
  Timer,
  UserRound,
  X,
} from 'lucide-react';
import { useState, type FormEvent, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { FormError } from '@/components/AuthLayout';
import { PageHeader } from '@/components/PageHeader';
import { PageLoader } from '@/components/PageLoader';
import { PasswordInput } from '@/components/PasswordInput';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardFooter, CardHeader } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Field, Input, Select } from '@/components/ui/input';
import { SwitchRow } from '@/components/ui/switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useSaveTheme } from '@/hooks/useSaveTheme';
import { api, errorMessage } from '@/lib/api';
import { useUser } from '@/lib/auth';
import { qk } from '@/lib/queryClient';
import { useTheme } from '@/lib/theme';
import type { Settings, SettingsUpdate, TestNotificationResult, Theme } from '@/lib/types';
import { cn, isValidUrl } from '@/lib/utils';

type Tab = 'notifications' | 'defaults' | 'appearance' | 'account';
const TABS: Tab[] = ['notifications', 'defaults', 'appearance', 'account'];

export default function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const tab = (TABS.includes(params.get('tab') as Tab) ? params.get('tab') : 'notifications') as Tab;
  const settings = useQuery({ queryKey: qk.settings, queryFn: api.settings });

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader title="Settings" description="Notifications, defaults for new items, and your account." />
      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v }, { replace: true })}>
        <TabsList className="w-full sm:w-auto">
          <TabsTrigger value="notifications" className="flex-1 sm:flex-none">
            <Bell /> <span className="hidden min-[400px]:inline">Notifications</span>
          </TabsTrigger>
          <TabsTrigger value="defaults" className="flex-1 sm:flex-none">
            <SlidersHorizontal /> <span className="hidden min-[400px]:inline">Defaults</span>
          </TabsTrigger>
          <TabsTrigger value="appearance" className="flex-1 sm:flex-none">
            <Palette /> <span className="hidden min-[400px]:inline">Appearance</span>
          </TabsTrigger>
          <TabsTrigger value="account" className="flex-1 sm:flex-none">
            <UserRound /> <span className="hidden min-[400px]:inline">Account</span>
          </TabsTrigger>
        </TabsList>

        {settings.isLoading ? (
          <PageLoader />
        ) : settings.isError || !settings.data ? (
          <EmptyState
            className="mt-6"
            icon={<AlertTriangle />}
            title="Couldn't load settings"
            description={errorMessage(settings.error)}
            action={<Button onClick={() => settings.refetch()}>Retry</Button>}
          />
        ) : (
          <>
            <TabsContent value="notifications">
              <NotificationsTab settings={settings.data} key={JSON.stringify(settings.data)} />
            </TabsContent>
            <TabsContent value="defaults">
              <DefaultsTab settings={settings.data} key={JSON.stringify(settings.data)} />
            </TabsContent>
            <TabsContent value="appearance">
              <AppearanceTab />
            </TabsContent>
            <TabsContent value="account">
              <AccountTab />
            </TabsContent>
          </>
        )}
      </Tabs>
    </div>
  );
}

function useSaveSettings(successMsg = 'Settings saved') {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SettingsUpdate) => api.updateSettings(body),
    onSuccess: (s) => {
      qc.setQueryData(qk.settings, s);
      if (successMsg) toast.success(successMsg);
    },
    onError: (e) => toast.error("Couldn't save settings", { description: errorMessage(e) }),
  });
}

/* --------------------------------------------------------- Notifications */

const TOPIC_RE = /^[A-Za-z0-9_-]{1,64}$/;
const PRIORITIES = [
  { v: 1, label: 'Min — no sound or vibration' },
  { v: 2, label: 'Low — no sound' },
  { v: 3, label: 'Default' },
  { v: 4, label: 'High — long vibration' },
  { v: 5, label: 'Urgent — breaks through Do Not Disturb' },
];

function randomTopic() {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return `stockwatch-${Array.from(bytes, (b) => b.toString(36).padStart(2, '0')).join('').slice(0, 10)}`;
}

type TokenMode = 'keep' | 'replace' | 'clear';

function NotificationsTab({ settings }: { settings: Settings }) {
  const [server, setServer] = useState(settings.ntfy_server || 'https://ntfy.sh');
  const [topic, setTopic] = useState(settings.ntfy_topic ?? '');
  const [priority, setPriority] = useState(settings.ntfy_priority ?? 4);
  const [outOfStock, setOutOfStock] = useState(settings.notify_on_out_of_stock);
  const [tokenMode, setTokenMode] = useState<TokenMode>(settings.ntfy_token_set ? 'keep' : 'replace');
  const [token, setToken] = useState('');
  const [testResult, setTestResult] = useState<TestNotificationResult | null>(null);
  const save = useSaveSettings();

  const serverErr = server.trim() && !isValidUrl(server.trim()) ? 'Enter a valid http(s) URL' : undefined;
  const topicErr = topic.trim() && !TOPIC_RE.test(topic.trim()) ? 'Letters, numbers, - and _ only (max 64)' : undefined;

  const body = (): SettingsUpdate => {
    const b: SettingsUpdate = {
      ntfy_server: server.trim().replace(/\/+$/, '') || 'https://ntfy.sh',
      ntfy_topic: topic.trim() || null,
      ntfy_priority: priority,
      notify_on_out_of_stock: outOfStock,
    };
    if (tokenMode === 'clear') b.ntfy_token = '';
    else if (tokenMode === 'replace' && token.trim()) b.ntfy_token = token.trim();
    return b;
  };

  const dirty =
    server.trim().replace(/\/+$/, '') !== (settings.ntfy_server ?? '').replace(/\/+$/, '') ||
    (topic.trim() || null) !== (settings.ntfy_topic || null) ||
    priority !== settings.ntfy_priority ||
    outOfStock !== settings.notify_on_out_of_stock ||
    tokenMode === 'clear' ||
    (tokenMode === 'replace' && !!token.trim());

  const test = useMutation({
    mutationFn: async () => {
      if (dirty) await api.updateSettings(body());
      return api.testNotification();
    },
    onSuccess: (r) => {
      setTestResult(r);
      if (r.ok) toast.success('Test notification sent', { description: 'Check your phone!' });
      else toast.error('Test notification failed', { description: r.error ?? undefined });
    },
    onError: (e) => setTestResult({ ok: false, error: errorMessage(e) }),
  });
  const qc = useQueryClient();

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (serverErr || topicErr) return;
    save.mutate(body());
  };

  const effectiveServer = server.trim().replace(/\/+$/, '') || 'https://ntfy.sh';
  const isPublicNtfy = /^https?:\/\/ntfy\.sh$/i.test(effectiveServer);

  return (
    <div className="space-y-6">
      <form onSubmit={onSubmit} noValidate>
        <Card>
          <CardHeader
            icon={<Send />}
            title="ntfy push notifications"
            description="Alerts are pushed to your phone through an ntfy topic."
            actions={
              settings.ntfy_topic ? (
                <Badge tone="green">
                  <CheckCircle2 /> Configured
                </Badge>
              ) : (
                <Badge tone="amber">Not set up</Badge>
              )
            }
          />
          <CardBody className="space-y-5">
            <div className="grid gap-5 sm:grid-cols-2">
              <Field label="Server" htmlFor="ntfy-server" error={serverErr} hint="Use https://ntfy.sh or your own server.">
                <Input
                  id="ntfy-server"
                  type="url"
                  inputMode="url"
                  value={server}
                  onChange={(e) => setServer(e.target.value)}
                  leading={<Server />}
                  invalid={!!serverErr}
                  placeholder="https://ntfy.sh"
                />
              </Field>
              <Field
                label="Topic"
                htmlFor="ntfy-topic"
                error={topicErr}
                hint={isPublicNtfy ? 'Anyone who knows a topic on ntfy.sh can read it — pick something hard to guess.' : 'Letters, numbers, - and _.'}
                aside={
                  <button
                    type="button"
                    onClick={() => setTopic(randomTopic())}
                    className="inline-flex items-center gap-1 rounded text-xs font-medium text-indigo-600 hover:text-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 dark:text-indigo-400"
                  >
                    <Dices className="size-3.5" /> Generate
                  </button>
                }
              >
                <Input
                  id="ntfy-topic"
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  placeholder="my-secret-restocks"
                  autoCapitalize="none"
                  spellCheck={false}
                  className="font-mono text-[13px]"
                  invalid={!!topicErr}
                  maxLength={64}
                />
              </Field>
            </div>

            <Field
              label="Access token"
              htmlFor="ntfy-token"
              hint="Only needed if your ntfy server requires authentication. Stored securely and never shown again."
            >
              {tokenMode === 'keep' ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="inline-flex h-9 items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-50 px-3 text-sm font-medium text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400">
                    <KeyRound className="size-4" /> Token set
                  </span>
                  <Button size="sm" onClick={() => setTokenMode('replace')}>
                    Replace
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setTokenMode('clear')}>
                    <X /> Remove
                  </Button>
                </div>
              ) : tokenMode === 'clear' ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="inline-flex h-9 items-center gap-2 rounded-lg border border-rose-500/30 bg-rose-50 px-3 text-sm text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">
                    Token will be removed when you save
                  </span>
                  <Button size="sm" variant="ghost" onClick={() => setTokenMode('keep')}>
                    Undo
                  </Button>
                </div>
              ) : (
                <div className="flex gap-2">
                  <div className="flex-1">
                    <PasswordInput
                      id="ntfy-token"
                      value={token}
                      onChange={(e) => setToken(e.target.value)}
                      placeholder={settings.ntfy_token_set ? 'New token' : 'tk_… (optional)'}
                      autoComplete="off"
                      className="font-mono text-[13px]"
                    />
                  </div>
                  {settings.ntfy_token_set && (
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setToken('');
                        setTokenMode('keep');
                      }}
                    >
                      Cancel
                    </Button>
                  )}
                </div>
              )}
            </Field>

            <Field label="Priority" htmlFor="ntfy-priority">
              <Select id="ntfy-priority" value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
                {PRIORITIES.map((p) => (
                  <option key={p.v} value={p.v}>
                    {p.v} · {p.label}
                  </option>
                ))}
              </Select>
            </Field>

            <div className="rounded-xl border border-zinc-200 p-4 dark:border-zinc-800">
              <SwitchRow
                id="oos"
                title="Notify when items go out of stock"
                description="Also send an alert when something you're watching sells out again."
                checked={outOfStock}
                onCheckedChange={setOutOfStock}
              />
            </div>

            {testResult && (
              <div
                role="status"
                className={cn(
                  'flex items-start gap-2.5 rounded-lg border px-3 py-2.5 text-sm',
                  testResult.ok
                    ? 'border-emerald-300/60 bg-emerald-50 text-emerald-800 dark:border-emerald-500/20 dark:bg-emerald-500/10 dark:text-emerald-300'
                    : 'border-rose-300/60 bg-rose-50 text-rose-800 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300',
                )}
              >
                {testResult.ok ? <CheckCircle2 className="mt-0.5 size-4 shrink-0" /> : <AlertTriangle className="mt-0.5 size-4 shrink-0" />}
                <span className="min-w-0 break-words">
                  {testResult.ok ? 'Test notification delivered to ntfy. It should appear on your phone within seconds.' : testResult.error || 'Delivery failed.'}
                </span>
              </div>
            )}
          </CardBody>
          <CardFooter className="justify-between">
            <Button
              variant="outline"
              loading={test.isPending}
              disabled={!topic.trim() || !!topicErr || !!serverErr}
              onClick={() => {
                setTestResult(null);
                test.mutate(undefined, { onSettled: () => dirty && qc.invalidateQueries({ queryKey: qk.settings }) });
              }}
            >
              {!test.isPending && <Send />} Send test
            </Button>
            <Button type="submit" variant="primary" loading={save.isPending} disabled={!dirty || !!serverErr || !!topicErr}>
              Save
            </Button>
          </CardFooter>
        </Card>
      </form>

      <Card>
        <CardHeader icon={<Smartphone />} title="Get alerts on your phone" description="One-time setup, takes a minute." />
        <CardBody>
          <ol className="space-y-4">
            <Step n={1} title="Install the ntfy app">
              Free on the{' '}
              <a className="font-medium text-indigo-600 hover:underline dark:text-indigo-400" href="https://apps.apple.com/app/ntfy/id1625396347" target="_blank" rel="noreferrer">
                App Store
              </a>{' '}
              and{' '}
              <a className="font-medium text-indigo-600 hover:underline dark:text-indigo-400" href="https://play.google.com/store/apps/details?id=io.heckel.ntfy" target="_blank" rel="noreferrer">
                Google Play
              </a>
              .
            </Step>
            <Step n={2} title="Subscribe to your topic">
              Tap <span className="font-medium">+</span>, enter the topic{' '}
              <code className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-xs text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200">
                {topic.trim() || 'your-topic'}
              </code>
              {!isPublicNtfy && (
                <>
                  , enable <span className="font-medium">Use another server</span> and enter{' '}
                  <code className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-xs text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200">
                    {effectiveServer}
                  </code>
                </>
              )}
              .
            </Step>
            <Step n={3} title="Send a test">
              Save your settings above and tap <span className="font-medium">Send test</span>. On iOS, allow notifications for ntfy when asked.
            </Step>
          </ol>
        </CardBody>
      </Card>
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-indigo-100 text-xs font-semibold text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300">
        {n}
      </span>
      <div className="min-w-0 pt-0.5">
        <p className="text-sm font-medium text-zinc-900 dark:text-zinc-100">{title}</p>
        <p className="mt-0.5 text-sm text-zinc-500 dark:text-zinc-400">{children}</p>
      </div>
    </li>
  );
}

/* --------------------------------------------------------------- Defaults */

function DefaultsTab({ settings }: { settings: Settings }) {
  const [interval, setInterval] = useState(String(settings.default_interval_minutes));
  const [zip, setZip] = useState(settings.default_zip ?? '');
  const [dist, setDist] = useState(String(settings.default_max_distance_miles));
  const save = useSaveSettings('Defaults saved');

  const iNum = Number(interval);
  const dNum = Number(dist);
  const intervalErr = !Number.isFinite(iNum) || iNum < 1 || iNum > 1440 ? 'Between 1 and 1440 minutes' : undefined;
  const distErr = !Number.isFinite(dNum) || dNum <= 0 ? 'Enter a positive distance' : undefined;
  const dirty =
    iNum !== settings.default_interval_minutes || (zip.trim() || null) !== (settings.default_zip || null) || dNum !== settings.default_max_distance_miles;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (intervalErr || distErr) return;
        save.mutate({ default_interval_minutes: iNum, default_zip: zip.trim() || null, default_max_distance_miles: dNum });
      }}
      noValidate
    >
      <Card>
        <CardHeader icon={<SlidersHorizontal />} title="Defaults for new items" description="Pre-filled when you add something new. Existing items keep their own settings." />
        <CardBody className="space-y-5">
          <Field label="Check interval" htmlFor="d-interval" error={intervalErr} hint="How often each item is checked. Shorter = faster alerts, more requests.">
            <div className="relative sm:w-48">
              <Input
                id="d-interval"
                type="number"
                inputMode="numeric"
                min={1}
                max={1440}
                value={interval}
                onChange={(e) => setInterval(e.target.value)}
                leading={<Timer />}
                className="pr-14 tabular-nums"
                invalid={!!intervalErr}
              />
              <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm text-zinc-400">min</span>
            </div>
          </Field>
          <div className="grid gap-5 sm:grid-cols-2">
            <Field label="Default ZIP code" htmlFor="d-zip" hint="Used for Apple in-store pickup searches.">
              <Input
                id="d-zip"
                inputMode="numeric"
                autoComplete="postal-code"
                maxLength={10}
                value={zip}
                onChange={(e) => setZip(e.target.value)}
                leading={<MapPin />}
                placeholder="95014"
              />
            </Field>
            <Field label="Default max distance" htmlFor="d-dist" error={distErr}>
              <div className="relative">
                <Input
                  id="d-dist"
                  type="number"
                  inputMode="numeric"
                  min={1}
                  value={dist}
                  onChange={(e) => setDist(e.target.value)}
                  className="pr-12 tabular-nums"
                  invalid={!!distErr}
                />
                <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm text-zinc-400">mi</span>
              </div>
            </Field>
          </div>
        </CardBody>
        <CardFooter>
          <Button type="submit" variant="primary" loading={save.isPending} disabled={!dirty || !!intervalErr || !!distErr}>
            Save
          </Button>
        </CardFooter>
      </Card>
    </form>
  );
}

/* ------------------------------------------------------------- Appearance */

const THEMES: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
];

function ThemePreview({ variant }: { variant: 'light' | 'dark' | 'system' }) {
  const pane = (dark: boolean) => (
    <div className={cn('flex h-full flex-1 gap-1.5 p-2', dark ? 'bg-zinc-950' : 'bg-zinc-50')}>
      <div className={cn('w-1/4 rounded', dark ? 'bg-zinc-900' : 'bg-white ring-1 ring-zinc-200')} />
      <div className="flex flex-1 flex-col gap-1.5">
        <div className={cn('h-2 w-1/2 rounded-sm', dark ? 'bg-zinc-700' : 'bg-zinc-300')} />
        <div className="grid flex-1 grid-cols-2 gap-1.5">
          <div className={cn('rounded', dark ? 'bg-zinc-900' : 'bg-white ring-1 ring-zinc-200')} />
          <div className={cn('rounded', dark ? 'bg-zinc-900' : 'bg-white ring-1 ring-zinc-200')}>
            <div className="m-1 h-1.5 w-1/2 rounded-sm bg-emerald-500/80" />
          </div>
        </div>
      </div>
    </div>
  );
  return (
    <div className="flex h-24 overflow-hidden rounded-lg">
      {variant === 'system' ? (
        <>
          {pane(false)}
          {pane(true)}
        </>
      ) : (
        pane(variant === 'dark')
      )}
    </div>
  );
}

function AppearanceTab() {
  const { theme } = useTheme();
  const saveTheme = useSaveTheme();
  return (
    <Card>
      <CardHeader icon={<Palette />} title="Theme" description="Choose how Stock Watcher looks. Saved to your account." />
      <CardBody>
        <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-3" role="radiogroup" aria-label="Theme">
          {THEMES.map((t) => {
            const active = theme === t.value;
            return (
              <button
                key={t.value}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => saveTheme(t.value)}
                className={cn(
                  'rounded-xl border p-2 text-left transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60',
                  active
                    ? 'border-indigo-500 ring-1 ring-indigo-500'
                    : 'border-zinc-200 hover:border-zinc-300 dark:border-zinc-800 dark:hover:border-zinc-700',
                )}
              >
                <ThemePreview variant={t.value} />
                <span className="mt-2 flex items-center gap-2 px-1 pb-0.5 text-sm font-medium text-zinc-800 dark:text-zinc-200">
                  <t.icon className="size-4 text-zinc-400" /> {t.label}
                  {active && <CheckCircle2 className="ml-auto size-4 text-indigo-500" />}
                </span>
              </button>
            );
          })}
        </div>
      </CardBody>
    </Card>
  );
}

/* ---------------------------------------------------------------- Account */

function AccountTab() {
  const user = useUser();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [touched, setTouched] = useState(false);
  const change = useMutation({
    mutationFn: () => api.changePassword(current, next),
    onSuccess: () => {
      toast.success('Password changed');
      setCurrent('');
      setNext('');
      setConfirm('');
      setTouched(false);
    },
  });
  const shortErr = next && next.length < 8 ? 'At least 8 characters' : undefined;
  const mismatch = confirm && confirm !== next ? "Passwords don't match" : undefined;
  const valid = current && next.length >= 8 && confirm === next;

  return (
    <div className="space-y-6">
      <Card>
        <CardBody className="flex items-center gap-4">
          <span className="flex size-12 items-center justify-center rounded-full bg-gradient-to-br from-indigo-400 to-violet-600 text-base font-semibold uppercase text-white">
            {user?.username.slice(0, 2)}
          </span>
          <div className="min-w-0">
            <p className="truncate font-semibold text-zinc-900 dark:text-zinc-50">{user?.username}</p>
            <p className="text-sm text-zinc-500 dark:text-zinc-400">{user?.is_admin ? 'Administrator' : 'Member'}</p>
          </div>
        </CardBody>
      </Card>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setTouched(true);
          if (valid) change.mutate();
        }}
        noValidate
      >
        <Card>
          <CardHeader icon={<Lock />} title="Change password" />
          <CardBody className="space-y-4">
            <FormError>{change.error ? errorMessage(change.error) : null}</FormError>
            <input type="text" name="username" autoComplete="username" value={user?.username ?? ''} readOnly hidden />
            <Field label="Current password" htmlFor="cur-pw" error={touched && !current ? 'Required' : undefined}>
              <PasswordInput id="cur-pw" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="New password" htmlFor="new-pw" error={touched ? shortErr : undefined} hint="At least 8 characters.">
                <PasswordInput id="new-pw" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} invalid={touched && !!shortErr} />
              </Field>
              <Field label="Confirm new password" htmlFor="confirm-pw" error={mismatch || undefined}>
                <PasswordInput id="confirm-pw" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} invalid={!!mismatch} />
              </Field>
            </div>
          </CardBody>
          <CardFooter>
            <Button type="submit" variant="primary" loading={change.isPending}>
              Update password
            </Button>
          </CardFooter>
        </Card>
      </form>
    </div>
  );
}
