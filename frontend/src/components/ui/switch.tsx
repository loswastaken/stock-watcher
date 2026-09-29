import * as SwitchPrimitive from '@radix-ui/react-switch';
import { forwardRef, type ReactNode } from 'react';
import { cn } from '@/lib/utils';

export const Switch = forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      'peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors',
      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:focus-visible:ring-offset-zinc-950',
      'disabled:cursor-not-allowed disabled:opacity-50',
      'data-[state=checked]:bg-indigo-600 data-[state=unchecked]:bg-zinc-300 dark:data-[state=checked]:bg-indigo-500 dark:data-[state=unchecked]:bg-zinc-700',
      className,
    )}
    {...props}
  >
    <SwitchPrimitive.Thumb className="pointer-events-none block size-4 rounded-full bg-white shadow-md ring-0 transition-transform data-[state=checked]:translate-x-4 data-[state=unchecked]:translate-x-0" />
  </SwitchPrimitive.Root>
));
Switch.displayName = 'Switch';

/** A row with title/description on the left and a switch on the right. */
export function SwitchRow({
  id,
  title,
  description,
  checked,
  onCheckedChange,
  disabled,
  icon,
}: {
  id: string;
  title: ReactNode;
  description?: ReactNode;
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  disabled?: boolean;
  icon?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-1">
      <div className="flex min-w-0 gap-3">
        {icon && <div className="mt-0.5 text-zinc-400 [&_svg]:size-4">{icon}</div>}
        <div className="min-w-0">
          <label htmlFor={id} className="cursor-pointer text-sm font-medium text-zinc-800 dark:text-zinc-200">
            {title}
          </label>
          {description && <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">{description}</p>}
        </div>
      </div>
      <Switch id={id} checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} className="mt-0.5" />
    </div>
  );
}
