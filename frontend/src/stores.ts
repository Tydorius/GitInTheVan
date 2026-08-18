import { writable, get } from 'svelte/store';
import { getToken, clearToken } from './api';
import { api } from './api';
import type { CertIPCheck } from './api';

export const isAuthenticated = writable(!!getToken());
export const currentRoute = writable(window.location.hash.slice(1) || '/');
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
