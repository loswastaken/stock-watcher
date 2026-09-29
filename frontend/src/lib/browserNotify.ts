/** Desktop/browser notifications for new alerts (while Stock Watcher is open in a tab). */
import type { Notification as Alert } from './types';

const KEY = 'sw-browser-notify';

export function browserNotifySupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window && window.isSecureContext;
}

export function browserNotifyEnabled(): boolean {
  if (!browserNotifySupported() || Notification.permission !== 'granted') return false;
  try {
    return localStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}

export function setBrowserNotify(on: boolean) {
  try {
    if (on) localStorage.setItem(KEY, '1');
    else localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: setting just won't persist */
  }
  window.dispatchEvent(new Event('sw-browser-notify'));
}

/** Ask for permission (must be called from a click) and turn the setting on if granted. */
export async function enableBrowserNotify(): Promise<NotificationPermission> {
  const perm = Notification.permission === 'default' ? await Notification.requestPermission() : Notification.permission;
  setBrowserNotify(perm === 'granted');
  return perm;
}

export function showBrowserNotification(n: Alert, open: (path: string) => void) {
  if (!browserNotifyEnabled()) return;
  try {
    const note = new Notification(n.title, {
      body: n.message,
      icon: n.image_url || '/favicon.svg',
      tag: `sw-alert-${n.id}`,
    });
    note.onclick = () => {
      window.focus();
      open(n.item_id != null ? `/items/${n.item_id}` : '/notifications');
      note.close();
    };
  } catch {
    /* some browsers (e.g. Android Chrome) only allow notifications from a service worker */
  }
}
