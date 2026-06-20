import { writable } from 'svelte/store';
import { getToken, clearToken } from './api';

export const isAuthenticated = writable(!!getToken());
export const currentRoute = writable(window.location.hash.slice(1) || '/');

export function logout() {
  clearToken();
  isAuthenticated.set(false);
  window.location.hash = '#/login';
}

window.addEventListener('hashchange', () => {
  currentRoute.set(window.location.hash.slice(1) || '/');
});
