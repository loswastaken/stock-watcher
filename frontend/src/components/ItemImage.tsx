import { ImageOff, Package } from 'lucide-react';
import { useEffect, useState } from 'react';
import { cn } from '@/lib/utils';

export function ItemImage({
  src,
  alt,
  className,
  imgClassName,
  iconClassName,
}: {
  src: string | null | undefined;
  alt: string;
  className?: string;
  imgClassName?: string;
  iconClassName?: string;
}) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    setFailed(false);
    setLoaded(false);
  }, [src]);

  const showImg = src && !failed;
  return (
    <div
      className={cn(
        'relative flex items-center justify-center overflow-hidden',
        // Shop photos almost always sit on white; give them a light "stage" in both themes and
        // multiply-blend so the white background melts into it.
        showImg
          ? 'bg-gradient-to-b from-white to-zinc-100 dark:from-zinc-200 dark:to-zinc-300'
          : 'bg-gradient-to-b from-zinc-50 to-zinc-100 dark:from-zinc-800/40 dark:to-zinc-900/60',
        className,
      )}
    >
      {showImg ? (
        <img
          src={src}
          alt={alt}
          loading="lazy"
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
          className={cn(
            'h-full w-full object-contain p-[8%] transition-opacity duration-300',
            'mix-blend-multiply',
            loaded ? 'opacity-100' : 'opacity-0',
            imgClassName,
          )}
        />
      ) : (
        <div className="flex flex-col items-center gap-1 text-zinc-300 dark:text-zinc-700">
          {failed ? (
            <ImageOff className={cn('size-8', iconClassName)} strokeWidth={1.5} />
          ) : (
            <Package className={cn('size-8', iconClassName)} strokeWidth={1.5} />
          )}
        </div>
      )}
    </div>
  );
}
