import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  className,
  ariaLabel,
  size = 'md',
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode; icon?: ReactNode }[];
  className?: string;
  ariaLabel?: string;
  size?: 'sm' | 'md';
}) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        'inline-flex w-full rounded-xl bg-zinc-100 p-1 dark:bg-zinc-900 dark:ring-1 dark:ring-zinc-800 sm:w-auto',
        className,
      )}
      onKeyDown={(e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        const i = options.findIndex((o) => o.value === value);
        const n = (i + (e.key === 'ArrowRight' ? 1 : -1) + options.length) % options.length;
        onChange(options[n].value);
        const btns = (e.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('button');
        btns[n]?.focus();
      }}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(o.value)}
            className={cn(
              'inline-flex flex-1 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg font-medium transition-all sm:flex-none',
              size === 'sm' ? 'h-7 px-2.5 text-xs' : 'h-8 px-3 text-sm',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 [&_svg]:size-4',
              active
                ? 'bg-white text-zinc-900 shadow-soft dark:bg-zinc-800 dark:text-zinc-50'
                : 'text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100',
            )}
          >
            {o.icon}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
