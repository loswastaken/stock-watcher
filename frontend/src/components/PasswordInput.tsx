import { Eye, EyeOff } from 'lucide-react';
import { forwardRef, useState } from 'react';
import { Input, type InputProps } from './ui/input';

export const PasswordInput = forwardRef<HTMLInputElement, Omit<InputProps, 'type' | 'trailing'>>((props, ref) => {
  const [show, setShow] = useState(false);
  return (
    <Input
      ref={ref}
      type={show ? 'text' : 'password'}
      {...props}
      trailing={
        <button
          type="button"
          onClick={() => setShow((s) => !s)}
          className="rounded p-1 text-zinc-400 hover:text-zinc-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 dark:hover:text-zinc-200"
          aria-label={show ? 'Hide password' : 'Show password'}
          tabIndex={-1}
        >
          {show ? <EyeOff /> : <Eye />}
        </button>
      }
    />
  );
});
PasswordInput.displayName = 'PasswordInput';
