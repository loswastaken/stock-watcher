import type { ReactNode } from 'react';
import { Logo } from './Logo';

export function AuthLayout({ title, subtitle, children, footer }: { title: string; subtitle?: ReactNode; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="relative flex min-h-dvh flex-col items-center justify-center overflow-hidden bg-zinc-50 px-4 py-12 dark:bg-zinc-950">
      {/* soft backdrop */}
      <div
        aria-hidden
        className="pointer-events-none absolute left-1/2 top-[-10%] h-[480px] w-[720px] -translate-x-1/2 rounded-full bg-indigo-500/10 blur-3xl dark:bg-indigo-500/[0.12]"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,rgb(0_0_0/0.03)_1px,transparent_1px),linear-gradient(to_bottom,rgb(0_0_0/0.03)_1px,transparent_1px)] bg-[size:48px_48px] [mask-image:radial-gradient(ellipse_at_center,black_20%,transparent_70%)] dark:bg-[linear-gradient(to_right,rgb(255_255_255/0.03)_1px,transparent_1px),linear-gradient(to_bottom,rgb(255_255_255/0.03)_1px,transparent_1px)]"
      />
      <div className="relative w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo className="mb-5 size-12 drop-shadow-lg" />
          <h1 className="text-xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">{title}</h1>
          {subtitle && <p className="mt-1.5 text-sm text-zinc-500 dark:text-zinc-400">{subtitle}</p>}
        </div>
        <div className="rounded-2xl border border-zinc-200 bg-white/80 p-6 shadow-xl shadow-zinc-900/[0.03] backdrop-blur dark:border-zinc-800 dark:bg-zinc-900/60 dark:shadow-black/20">
          {children}
        </div>
        {footer && <div className="mt-6 text-center text-xs text-zinc-500 dark:text-zinc-500">{footer}</div>}
      </div>
    </div>
  );
}

export function FormError({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300">
      {children}
    </div>
  );
}
