import { Bell, BellOff, ExternalLink, ImageDown, MoreHorizontal, Pause, Pencil, Play, RefreshCw, Trash2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useItemActions } from '@/hooks/useItemActions';
import type { Item } from '@/lib/types';
import { cn } from '@/lib/utils';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '../ui/dropdown-menu';

export function ItemActionsMenu({
  item,
  className,
  onDeleted,
  extended,
}: {
  item: Item;
  className?: string;
  onDeleted?: () => void;
  extended?: boolean;
}) {
  const navigate = useNavigate();
  const a = useItemActions();
  const checking = a.check.isPending && a.check.variables?.id === item.id;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Actions for ${item.name}`}
          className={cn(
            'flex size-8 items-center justify-center rounded-lg text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/60 data-[state=open]:bg-zinc-100 data-[state=open]:text-zinc-900',
            'dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100 dark:data-[state=open]:bg-zinc-800 dark:data-[state=open]:text-zinc-100',
            className,
          )}
          onClick={(e) => e.stopPropagation()}
        >
          <MoreHorizontal className="size-4" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent onClick={(e) => e.stopPropagation()}>
        <DropdownMenuItem disabled={checking} onSelect={() => a.check.mutate(item)}>
          <RefreshCw className={cn(checking && 'animate-spin')} /> {checking ? 'Checking…' : 'Check now'}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => a.toggle.mutate(item)}>
          {item.enabled ? (
            <>
              <Pause /> Pause watching
            </>
          ) : (
            <>
              <Play /> Resume watching
            </>
          )}
        </DropdownMenuItem>
        {extended && (
          <DropdownMenuItem onSelect={() => a.toggleNotify.mutate(item)}>
            {item.notify_enabled ? (
              <>
                <BellOff /> Mute notifications
              </>
            ) : (
              <>
                <Bell /> Enable notifications
              </>
            )}
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onSelect={() => navigate(`/items/${item.id}/edit`)}>
          <Pencil /> Edit
        </DropdownMenuItem>
        {extended && (
          <DropdownMenuItem onSelect={() => a.refreshImage.mutate(item)}>
            <ImageDown /> Refresh photo
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onSelect={() => window.open(item.url, '_blank', 'noopener,noreferrer')}>
          <ExternalLink /> Open product page
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          destructive
          onSelect={async () => {
            if (await a.confirmDelete(item)) onDeleted?.();
          }}
        >
          <Trash2 /> Delete
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
