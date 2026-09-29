import * as AD from '@radix-ui/react-alert-dialog';
import { AlertTriangle } from 'lucide-react';
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react';
import { Button } from './button';

export interface ConfirmOptions {
  title: ReactNode;
  description?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  destructive?: boolean;
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;
const ConfirmContext = createContext<ConfirmFn | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const [open, setOpen] = useState(false);
  const resolver = useRef<((v: boolean) => void) | null>(null);

  const confirm = useCallback<ConfirmFn>((o) => {
    setOpts(o);
    setOpen(true);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const close = (value: boolean) => {
    resolver.current?.(value);
    resolver.current = null;
    setOpen(false);
  };

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <AD.Root open={open} onOpenChange={(o) => !o && close(false)}>
        <AD.Portal>
          <AD.Overlay className="fixed inset-0 z-[60] animate-fade-in bg-zinc-950/50 backdrop-blur-[2px] dark:bg-black/60" />
          <AD.Content className="fixed left-1/2 top-1/2 z-[60] w-[calc(100vw-2rem)] max-w-sm -translate-x-1/2 -translate-y-1/2 animate-zoom-in rounded-2xl border border-zinc-200 bg-white p-5 shadow-2xl focus:outline-none dark:border-zinc-800 dark:bg-zinc-900">
            <div className="flex gap-4">
              {opts?.destructive && (
                <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-rose-100 text-rose-600 dark:bg-rose-500/15 dark:text-rose-400">
                  <AlertTriangle className="size-5" />
                </div>
              )}
              <div className="min-w-0">
                <AD.Title className="text-base font-semibold text-zinc-900 dark:text-zinc-50">{opts?.title}</AD.Title>
                <AD.Description className="mt-1.5 text-sm text-zinc-500 dark:text-zinc-400">
                  {opts?.description ?? 'Are you sure?'}
                </AD.Description>
              </div>
            </div>
            <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <AD.Cancel asChild>
                <Button variant="outline">{opts?.cancelText ?? 'Cancel'}</Button>
              </AD.Cancel>
              <AD.Action asChild>
                <Button variant={opts?.destructive ? 'danger' : 'primary'} onClick={() => close(true)}>
                  {opts?.confirmText ?? 'Confirm'}
                </Button>
              </AD.Action>
            </div>
          </AD.Content>
        </AD.Portal>
      </AD.Root>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error('useConfirm must be used inside ConfirmProvider');
  return ctx;
}
