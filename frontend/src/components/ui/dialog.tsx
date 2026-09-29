import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export function DialogOverlay({ className }: { className?: string }) {
  return (
    <DialogPrimitive.Overlay
      className={cn('fixed inset-0 z-50 animate-fade-in bg-zinc-950/50 backdrop-blur-[2px] dark:bg-black/60', className)}
    />
  );
}

export function DialogContent({
  title,
  description,
  children,
  footer,
  className,
  hideClose,
}: {
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  className?: string;
  hideClose?: boolean;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogOverlay />
      <DialogPrimitive.Content
        className={cn(
          'fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 animate-zoom-in flex-col',
          'rounded-2xl border border-zinc-200 bg-white shadow-2xl focus:outline-none dark:border-zinc-800 dark:bg-zinc-900',
          className,
        )}
      >
        <div className="flex items-start justify-between gap-4 px-5 pb-2 pt-5">
          <div className="min-w-0">
            <DialogPrimitive.Title className="text-base font-semibold text-zinc-900 dark:text-zinc-50">{title}</DialogPrimitive.Title>
            {description ? (
              <DialogPrimitive.Description className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
                {description}
              </DialogPrimitive.Description>
            ) : (
              <DialogPrimitive.Description className="sr-only">{typeof title === 'string' ? title : 'Dialog'}</DialogPrimitive.Description>
            )}
          </div>
          {!hideClose && (
            <DialogPrimitive.Close
              className="-mr-1 -mt-1 rounded-lg p-1.5 text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
              aria-label="Close"
            >
              <X className="size-4" />
            </DialogPrimitive.Close>
          )}
        </div>
        {children && <div className="min-h-0 overflow-y-auto px-5 py-3">{children}</div>}
        {footer && <div className="flex flex-col-reverse gap-2 px-5 pb-5 pt-2 sm:flex-row sm:justify-end">{footer}</div>}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

/** Side sheet (used for the mobile navigation). */
export function SheetContent({
  children,
  side = 'left',
  className,
  title,
}: {
  children: ReactNode;
  side?: 'left' | 'bottom';
  className?: string;
  title: string;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogOverlay />
      <DialogPrimitive.Content
        className={cn(
          'fixed z-50 flex flex-col bg-white shadow-2xl focus:outline-none dark:bg-zinc-950',
          side === 'left' &&
            'inset-y-0 left-0 w-[85vw] max-w-xs animate-slide-in-left border-r border-zinc-200 dark:border-zinc-800',
          side === 'bottom' &&
            'inset-x-0 bottom-0 max-h-[85dvh] animate-slide-in-bottom rounded-t-2xl border-t border-zinc-200 pb-[env(safe-area-inset-bottom)] dark:border-zinc-800',
          className,
        )}
      >
        <DialogPrimitive.Title className="sr-only">{title}</DialogPrimitive.Title>
        <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}
