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

/* ------------------------------------------------------------ alert sound */

let audioCtx: AudioContext | null = null;
let userGesture = false;

function hasUserGesture(): boolean {
  if (userGesture) return true;
  const ua = (navigator as Navigator & { userActivation?: { hasBeenActive: boolean } }).userActivation;
  return !!ua?.hasBeenActive;
}

/** The shared AudioContext, created on first use — and only after a user gesture, so the
 * browser never warns about an AudioContext that isn't allowed to start. */
function getAudioContext(): AudioContext | null {
  if (typeof window === 'undefined' || !hasUserGesture()) return null;
  if (audioCtx) return audioCtx;
  const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctx) return null;
  try {
    audioCtx = new Ctx();
  } catch {
    return null;
  }
  return audioCtx;
}

/**
 * Browsers only let a page make sound after a user gesture. Call once at startup: it records
 * the first pointerdown/keydown so later alert beeps may create/resume audio. Returns a cleanup.
 */
export function primeAlertSound(): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const unlock = () => {
    userGesture = true;
    // Resume an existing context from inside the gesture; don't create one just for this.
    if (audioCtx?.state === 'suspended') void audioCtx.resume().catch(() => undefined);
    cleanup();
  };
  const cleanup = () => {
    window.removeEventListener('pointerdown', unlock, true);
    window.removeEventListener('keydown', unlock, true);
  };
  window.addEventListener('pointerdown', unlock, true);
  window.addEventListener('keydown', unlock, true);
  return cleanup;
}

/** A short two-tone chime, synthesized with WebAudio (no audio asset). Silent until the user
 * has interacted with the page. Never throws. */
export function playAlertSound() {
  const ctx = getAudioContext();
  if (!ctx) return;
  try {
    if (ctx.state === 'suspended') void ctx.resume().catch(() => undefined);
    const start = ctx.currentTime + 0.01;
    [880, 1318.5].forEach((freq, i) => {
      const t = start + i * 0.16;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, t);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t);
      osc.stop(t + 0.24);
    });
  } catch {
    /* audio unavailable: silent */
  }
}
