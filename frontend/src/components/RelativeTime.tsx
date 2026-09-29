import { useNow } from '@/hooks/useNow';
import { absoluteTime, cn, relativeTime } from '@/lib/utils';
import { Tooltip } from './ui/tooltip';

export function RelativeTime({
  iso,
  className,
  prefix,
  fallback = 'never',
}: {
  iso: string | null | undefined;
  className?: string;
  prefix?: string;
  fallback?: string;
}) {
  const now = useNow();
  if (!iso) return <span className={className}>{prefix ? `${prefix} ${fallback}` : fallback}</span>;
  return (
    <Tooltip content={absoluteTime(iso)}>
      <time dateTime={iso} className={cn('tabular-nums', className)}>
        {prefix ? `${prefix} ` : ''}
        {relativeTime(iso, now)}
      </time>
    </Tooltip>
  );
}
