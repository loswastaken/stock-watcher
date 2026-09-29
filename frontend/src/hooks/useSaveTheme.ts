import { useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { api } from '@/lib/api';
import { qk } from '@/lib/queryClient';
import { useTheme } from '@/lib/theme';
import type { Settings, Theme } from '@/lib/types';

/** Apply a theme locally right away and persist it to the user's settings. */
export function useSaveTheme() {
  const { setTheme } = useTheme();
  const qc = useQueryClient();
  return useCallback(
    (t: Theme) => {
      setTheme(t);
      qc.setQueryData<Settings>(qk.settings, (s) => (s ? { ...s, theme: t } : s));
      api
        .updateSettings({ theme: t })
        .then((s) => qc.setQueryData(qk.settings, s))
        .catch(() => {
          /* local preference still applies */
        });
    },
    [setTheme, qc],
  );
}
