import { writable } from 'svelte/store';
import { getToken, clearToken } from './api';
import { api } from './api';

export const isAuthenticated = writable(!!getToken());
export const currentRoute = writable(window.location.hash.slice(1) || '/');
export const isAdmin = writable(false);

export function logout() {
  clearToken();
  isAuthenticated.set(false);
  isAdmin.set(false);
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
