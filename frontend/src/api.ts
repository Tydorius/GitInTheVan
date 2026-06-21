const API_BASE = '';

export function getToken(): string | null {
  return localStorage.getItem('gitv_token');
}

export function setToken(token: string): void {
  localStorage.setItem('gitv_token', token);
}

export function clearToken(): void {
  localStorage.removeItem('gitv_token');
}

export function getApiKey(): string | null {
  return localStorage.getItem('gitv_api_key');
}

export function setApiKey(key: string): void {
  localStorage.setItem('gitv_api_key', key);
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((options.headers as Record<string, string>) || {}),
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const resp = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (resp.status === 401) {
    clearToken();
    window.location.hash = '#/login';
    throw new Error('Unauthorized');
  }

  if (resp.status === 204) {
    return undefined as T;
  }

  const text = await resp.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }

  if (!resp.ok) {
    const detail = (data as any)?.detail;
    let msg: string;
    if (typeof detail === 'string') {
      msg = detail;
    } else if (Array.isArray(detail)) {
      msg = detail.map((e: any) => `${e.loc?.join('.') || e.msg}: ${e.msg || ''}`).join('; ');
    } else if (typeof detail === 'object' && detail) {
      msg = JSON.stringify(detail);
    } else {
      msg = resp.statusText;
    }
    throw new Error(msg || resp.statusText);
  }

  return data as T;
}

export const api = {
  // Auth
  setup: (username: string, password: string) =>
    request<{ access_token: string; api_key: string }>('/api/auth/setup', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  login: (username: string, password: string) =>
    request<{ access_token: string }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  getMe: () =>
    request<{ id: string; username: string; is_admin: boolean }>('/api/auth/me'),

  // Health
  health: () => request<{ status: string }>('/health'),

  // Users
  listUsers: () => request<{ users: any[] }>('/api/users'),
  createUser: (username: string, password: string) =>
    request<any>('/api/users', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  updateUser: (id: string, data: { username?: string; is_disabled?: boolean }) =>
    request<any>(`/api/users/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteUser: (id: string) =>
    request<void>(`/api/users/${id}`, { method: 'DELETE' }),
  resetUserPassword: (id: string, password: string) =>
    request<any>(`/api/users/${id}/reset-password`, { method: 'POST', body: JSON.stringify({ password }) }),
  regenerateUserApiKey: (id: string) =>
    request<{ api_key: string }>(`/api/users/${id}/regenerate-api-key`, { method: 'POST' }),

  // Endpoints
  listEndpoints: () => request<{ endpoints: any[] }>('/api/endpoints'),
  createEndpoint: (data: { name: string; base_url: string; api_key: string; api_base_path?: string; enabled?: boolean }) =>
    request<any>('/api/endpoints', { method: 'POST', body: JSON.stringify(data) }),
  updateEndpoint: (id: string, data: any) =>
    request<any>(`/api/endpoints/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteEndpoint: (id: string) =>
    request<void>(`/api/endpoints/${id}`, { method: 'DELETE' }),

  // Settings
  getSettings: () => request<{ default_endpoint_id: string | null; default_model: string; preserve_thinking: boolean; gitv_status: boolean; simulated_streaming_speed: number }>('/api/settings'),
  updateSettings: (data: any) =>
    request<any>('/api/settings', { method: 'PUT', body: JSON.stringify(data) }),

  // Cantrips
  listCantrips: () => request<{ cantrips: any[] }>('/api/cantrips'),
  createCantrip: (data: any) =>
    request<any>('/api/cantrips', { method: 'POST', body: JSON.stringify(data) }),
  getCantrip: (id: string) => request<any>(`/api/cantrips/${id}`),
  updateCantrip: (id: string, data: any) =>
    request<any>(`/api/cantrips/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteCantrip: (id: string) =>
    request<void>(`/api/cantrips/${id}`, { method: 'DELETE' }),
  testCantrip: (data: any) =>
    request<any>('/api/cantrips/test', { method: 'POST', body: JSON.stringify(data) }),
  testCantripById: (id: string, data: any) =>
    request<any>(`/api/cantrips/${id}/test`, { method: 'POST', body: JSON.stringify(data) }),
  validateCantrip: (code: string) =>
    request<{ valid: boolean; error: string | null }>('/api/cantrips/validate', { method: 'POST', body: JSON.stringify({ code }) }),
  listTemplates: () =>
    request<{ templates: any[] }>('/api/cantrips/templates'),
  installTemplate: (name: string) =>
    request<any>('/api/cantrips/templates/install', { method: 'POST', body: JSON.stringify({ template_name: name }) }),

  // Diagnostics
  runAudit: () => request<{ results: any[]; all_passed: boolean }>('/api/diagnostics/audit'),

  // Lorebooks
  listLorebooks: () => request<{ lorebooks: any[] }>('/api/lorebooks'),
  createLorebook: (data: any) =>
    request<any>('/api/lorebooks', { method: 'POST', body: JSON.stringify(data) }),
  getLorebook: (id: string) => request<any>(`/api/lorebooks/${id}`),
  updateLorebook: (id: string, data: any) =>
    request<any>(`/api/lorebooks/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteLorebook: (id: string) =>
    request<void>(`/api/lorebooks/${id}`, { method: 'DELETE' }),
  addLorebookEntry: (lbId: string, data: any) =>
    request<any>(`/api/lorebooks/${lbId}/entries`, { method: 'POST', body: JSON.stringify(data) }),
  updateLorebookEntry: (lbId: string, entryId: string, data: any) =>
    request<any>(`/api/lorebooks/${lbId}/entries/${entryId}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteLorebookEntry: (lbId: string, entryId: string) =>
    request<void>(`/api/lorebooks/${lbId}/entries/${entryId}`, { method: 'DELETE' }),
  importLorebook: (data: any) =>
    request<any>('/api/lorebooks/import', { method: 'POST', body: JSON.stringify(data) }),
  exportLorebook: (id: string) =>
    request<any>(`/api/lorebooks/${id}/export`),

  // Verification
  listVerificationRules: () => request<{ rules: any[] }>('/api/verification/rules'),
  createVerificationRule: (data: any) =>
    request<any>('/api/verification/rules', { method: 'POST', body: JSON.stringify(data) }),
  updateVerificationRule: (id: string, data: any) =>
    request<any>(`/api/verification/rules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteVerificationRule: (id: string) =>
    request<void>(`/api/verification/rules/${id}`, { method: 'DELETE' }),
  getVerificationSettings: () =>
    request<{ verification_enabled: boolean; verification_endpoint_id: string | null; verification_model: string }>('/api/verification/settings'),
  updateVerificationSettings: (data: any) =>
    request<any>('/api/verification/settings', { method: 'PUT', body: JSON.stringify(data) }),
  testVerification: (data: any) =>
    request<any>('/api/verification/test', { method: 'POST', body: JSON.stringify(data) }),
  listVerificationLogs: () => request<{ logs: any[]; total: number }>('/api/verification/logs'),

  // Memories
  listMemories: (conversationId?: string) =>
    request<{ memories: any[]; total: number }>(`/api/memories${conversationId ? `?conversation_id=${conversationId}` : ''}`),
  updateMemory: (id: string, value: string) =>
    request<any>(`/api/memories/${id}`, { method: 'PUT', body: JSON.stringify({ value }) }),
  deleteMemory: (id: string) =>
    request<void>(`/api/memories/${id}`, { method: 'DELETE' }),

  // Summarization
  getSummarizationSettings: () =>
    request<{
      summarization_enabled: boolean;
      summarization_endpoint_id: string | null;
      summarization_model: string;
      summarization_token_threshold: number;
      summarization_keep_recent: number;
      summarization_prompt: string;
    }>('/api/summarization/settings'),
  updateSummarizationSettings: (data: any) =>
    request<any>('/api/summarization/settings', { method: 'PUT', body: JSON.stringify(data) }),
  listSummaries: (internalChatId?: string) =>
    request<{ summaries: any[]; total: number }>(`/api/summarization/summaries${internalChatId ? `?internal_chat_id=${internalChatId}` : ''}`),
  deleteSummary: (id: string) =>
    request<void>(`/api/summarization/summaries/${id}`, { method: 'DELETE' }),

  // Forbidden Words
  getForbiddenSettings: () =>
    request<{ forbidden_words_enabled: boolean; forbidden_words_case_sensitive: boolean }>('/api/forbidden-words/settings'),
  updateForbiddenSettings: (data: any) =>
    request<any>('/api/forbidden-words/settings', { method: 'PUT', body: JSON.stringify(data) }),
  listForbiddenWords: () =>
    request<{ words: any[]; total: number }>('/api/forbidden-words'),
  createForbiddenWord: (phrase: string, is_regex: boolean = false) =>
    request<any>('/api/forbidden-words', { method: 'POST', body: JSON.stringify({ phrase, is_regex }) }),
  deleteForbiddenWord: (id: string) =>
    request<void>(`/api/forbidden-words/${id}`, { method: 'DELETE' }),
  testForbiddenWords: (content: string) =>
    request<{ has_matches: boolean; summary: string; match_count: number }>('/api/forbidden-words/test', { method: 'POST', body: JSON.stringify({ content }) }),

  // Tags - browse public tagged resources
  listPublicLorebooks: () => request<{ lorebooks: any[] }>('/api/lorebooks/public'),
  listPublicCantrips: () => request<{ cantrips: any[] }>('/api/cantrips/public'),
};
