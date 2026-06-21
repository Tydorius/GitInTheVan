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

window.addEventListener('hashchange', () => {
  currentRoute.set(window.location.hash.slice(1) || '/');
});
