import { writable, derived, get } from 'svelte/store';
import { getToken, clearToken } from './api';
import { api } from './api';
import type { CertIPCheck } from './api';
import { parseRoute } from './lib/deeplink';

export const isAuthenticated = writable(!!getToken());
export const currentRoute = writable(window.location.hash.slice(1) || '/');

/**
 * The current route split into its page and query parameters.
 *
 * Pages read this to open a specific object from a link, e.g.
 * `#/cantrips?id=abc`. Before this existed the query was stripped and ignored,
 * so every such link landed on the page with nothing selected.
 */
export const routeParams = derived(currentRoute, ($route) => parseRoute($route));
export const routePage = derived(currentRoute, ($route) => parseRoute($route).page);
export const isAdmin = writable(false);
export const siteBanner = writable<{ banner: string; level: string } | null>(null);
export const certIpWarning = writable<CertIPCheck | null>(null);

export async function loadSiteBanner() {
  try {
    const result = await api.getSiteBanner();
    siteBanner.set(result.banner ? result : null);
  } catch {
    siteBanner.set(null);
  }
}

/** Admin-only: the running certificate no longer covers any live LAN address. */
export async function loadCertIpWarning() {
  if (!get(isAdmin)) {
    certIpWarning.set(null);
    return;
  }
  try {
    const result = await api.getCertIPCheck();
    certIpWarning.set(result.mismatch && !result.acknowledged ? result : null);
  } catch {
    certIpWarning.set(null);
  }
}

export async function acknowledgeCertIpWarning(fingerprint: string) {
  const result = await api.acknowledgeCertIPCheck(fingerprint);
  certIpWarning.set(result.mismatch && !result.acknowledged ? result : null);
}

export function logout() {
  clearToken();
  isAuthenticated.set(false);
  isAdmin.set(false);
  certIpWarning.set(null);
  window.location.hash = '#/login';
}

export async function checkAdmin() {
  if (!getToken()) return;
  try {
    const me = await api.getMe();
    isAdmin.set(me.is_admin);
  } catch {
    isAdmin.set(false);
  }
  // Both the login and session-restore paths land here, so the cert banner
  // resolves as soon as admin status is known.
  await loadCertIpWarning();
}

export async function initializeAuth() {
  const token = getToken();
  if (!token) {
    isAuthenticated.set(false);
    isAdmin.set(false);
    return;
  }
  try {
    const resp = await fetch('/health');
    if (resp.ok) {
      const test = await fetch('/api/settings', {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (test.status === 401) {
        logout();
        return;
      }
    }
    isAuthenticated.set(true);
    await checkAdmin();
  } catch {
    isAuthenticated.set(false);
  }
}

window.addEventListener('hashchange', () => {
  currentRoute.set(window.location.hash.slice(1) || '/');
});
