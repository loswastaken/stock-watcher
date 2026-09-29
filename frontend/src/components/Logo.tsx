import { useId } from 'react';
import { cn } from '@/lib/utils';

export function Logo({ className }: { className?: string }) {
  // Unique gradient id per instance: a shared id breaks when the first instance is display:none.
  const gid = `sw-logo-${useId().replace(/:/g, '')}`;
  return (
    <svg viewBox="0 0 64 64" className={cn('size-8 shrink-0', className)} aria-hidden="true">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#818cf8" />
          <stop offset="1" stopColor="#6d28d9" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="15" fill={`url(#${gid})`} />
      <path d="M18 24l14-7 14 7v16l-14 7-14-7z" fill="none" stroke="#fff" strokeWidth="3.5" strokeLinejoin="round" />
      <path d="M18 24l14 7 14-7M32 31v16" fill="none" stroke="#fff" strokeWidth="3.5" strokeLinejoin="round" />
      <circle cx="47" cy="17" r="7" fill="#34d399" stroke="#fff" strokeWidth="3" />
    </svg>
  );
}

export function AppleLogo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 814 1000" className={cn('size-3.5', className)} fill="currentColor" aria-hidden="true">
      <path d="M788.1 340.9c-5.8 4.5-108.2 62.2-108.2 190.5 0 148.4 130.3 200.9 134.2 202.2-.6 3.2-20.7 71.9-68.7 141.9-42.8 61.6-87.5 123.1-155.5 123.1s-85.5-39.5-164-39.5c-76.5 0-103.7 40.8-165.9 40.8s-105.6-57-155.5-127C46.7 790.7 0 663 0 541.8c0-194.4 126.4-297.5 250.8-297.5 66.1 0 121.2 43.4 162.7 43.4 39.5 0 101.1-46 176.3-46 28.5 0 130.9 2.6 198.3 99.2zm-234-181.5c31.1-36.9 53.1-88.1 53.1-139.3 0-7.1-.6-14.3-1.9-20.1-50.6 1.9-110.8 33.7-147.1 75.8-28.5 32.4-55.1 83.6-55.1 135.5 0 7.8 1.3 15.6 1.9 18.1 3.2.6 8.4 1.3 13.6 1.3 45.4 0 102.5-30.4 135.5-71.3z" />
    </svg>
  );
}
