import * as DM from '@radix-ui/react-dropdown-menu';
import { forwardRef } from 'react';
import { cn } from '@/lib/utils';

export const DropdownMenu = DM.Root;
export const DropdownMenuTrigger = DM.Trigger;
export const DropdownMenuGroup = DM.Group;

export const DropdownMenuContent = forwardRef<
  React.ElementRef<typeof DM.Content>,
  React.ComponentPropsWithoutRef<typeof DM.Content>
>(({ className, sideOffset = 6, align = 'end', ...props }, ref) => (
  <DM.Portal>
    <DM.Content
      ref={ref}
      sideOffset={sideOffset}
      align={align}
      collisionPadding={8}
      className={cn(
        'z-50 min-w-[11rem] animate-slide-up overflow-hidden rounded-xl border border-zinc-200 bg-white p-1 text-sm shadow-lifted',
        'dark:border-zinc-800 dark:bg-zinc-900',
        className,
      )}
      {...props}
    />
  </DM.Portal>
));
DropdownMenuContent.displayName = 'DropdownMenuContent';

export const DropdownMenuItem = forwardRef<
  React.ElementRef<typeof DM.Item>,
  React.ComponentPropsWithoutRef<typeof DM.Item> & { destructive?: boolean }
>(({ className, destructive, ...props }, ref) => (
  <DM.Item
    ref={ref}
    className={cn(
      'relative flex cursor-pointer select-none items-center gap-2 rounded-lg px-2 py-1.5 text-zinc-700 outline-none transition-colors',
      'data-[highlighted]:bg-zinc-100 data-[highlighted]:text-zinc-900 data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
      'dark:text-zinc-300 dark:data-[highlighted]:bg-zinc-800 dark:data-[highlighted]:text-zinc-50',
      '[&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-zinc-400',
      destructive &&
        'text-rose-600 data-[highlighted]:bg-rose-50 data-[highlighted]:text-rose-700 dark:text-rose-400 dark:data-[highlighted]:bg-rose-500/10 dark:data-[highlighted]:text-rose-300 [&_svg]:text-current',
      className,
    )}
    {...props}
  />
));
DropdownMenuItem.displayName = 'DropdownMenuItem';

export function DropdownMenuSeparator({ className }: { className?: string }) {
  return <DM.Separator className={cn('-mx-1 my-1 h-px bg-zinc-100 dark:bg-zinc-800', className)} />;
}

export function DropdownMenuLabel({ className, ...props }: React.ComponentPropsWithoutRef<typeof DM.Label>) {
  return <DM.Label className={cn('px-2 py-1.5 text-xs font-medium text-zinc-500 dark:text-zinc-400', className)} {...props} />;
}
