const API_BASE = '';

export function getToken(): string | null {
  return localStorage.getItem('gitv_token');
}

export function setToken(token: string): void {
  localStorage.setItem('gitv_token', token);
}

export function clearToken(): void {
  localStorage.removeItem('gitv_token');
  // Note: API key is intentionally NOT cleared on logout.
  // It is a proxy key, not an auth credential. Clearing it forces
  // the user to regenerate it on every login since the server only
  // stores the hash and cannot return the original.
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

export interface CertIPCheck {
  mismatch: boolean;
  reason: string;
  cert_ips: string[];
  local_ips: string[];
  fingerprint: string;
  acknowledged: boolean;
}

/** Token, latency and efficiency figures for one debug run. */
export interface DebugRunTotals {
  llm_call_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  injected_tokens: number;
  /** 'upstream' | 'estimated' | 'mixed'. Never present an estimate as measured. */
  tokens_source: string;
  llm_latency_ms: number;
  total_latency_ms: number | null;
  /** Wall clock minus upstream: what this proxy's own pipeline cost. */
  overhead_ms: number | null;
  tokens_per_second: number | null;
  injection_overhead_pct: number | null;
}

export interface DebugExchangeListItem {
  id: string;
  chat_id: string;
  model: string;
  label: string;
  saved: boolean;
  saved_at: string;
  created_at: string;
  has_response: boolean;
  has_verification: boolean;
  stage_count: number;
  source: string;
  totals: DebugRunTotals;
}

export interface DebugListResponse {
  exchanges: DebugExchangeListItem[];
  saved_count: number;
  max_saved: number;
}

/** One cantrip a run considered, whether or not it fired. */
export interface DebugCantrip {
  id: string;
  name: string;
  position: string;
  triggered: boolean;
  reason?: string;
  tag?: string;
  code?: string;
  code_hash?: string;
  duration_ms?: number;
  debug_logs?: string[];
  error?: string;
  output?: Record<string, any>;
  fields_changed?: string[];
  /** Per-store deltas: what this cantrip wrote to chat/user/cantrip data. */
  data_changes?: Record<string, Record<string, { op: string; from?: any; to?: any }>>;
}

export interface DebugLlmCall {
  purpose: string;
  stage_index: number | null;
  endpoint_id: string;
  endpoint_name: string;
  provider: string;
  model_requested: string;
  model_resolved: string;
  failover_attempt: number;
  latency_ms: number;
  status_code: number;
  error: string;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  tokens_source: string;
}

export interface DebugStage {
  name: string;
  label: string;
  item_id: string | null;
  item_name: string | null;
  detail: string;
  setting: string;
  setting_value: any;
  messages_before?: string | null;
  messages_after?: string | null;
  content_before?: string;
  content_after?: string;
  /** Set when the trace exceeded the size cap and snapshots were shed. */
  messages_dropped?: boolean;
  metadata: Record<string, any>;
}

export interface DebugPipelineData {
  schema_version?: number;
  stages: DebugStage[];
  original_messages: string;
  tags: string[];
  truncated?: boolean;
  truncated_reason?: string;
  run: {
    started_at: string;
    source: string;
    replay_of: string;
    llm_calls: DebugLlmCall[];
    cantrips: DebugCantrip[];
    totals: Partial<DebugRunTotals>;
  };
}

export interface DebugExchange {
  id: string;
  chat_id: string;
  model: string;
  label: string;
  saved: boolean;
  saved_at: string;
  pipeline_data: DebugPipelineData;
  response_content: string;
  verification_data: Record<string, any>;
  created_at: string;
}

/** One line-level diff between a baseline and another run. */
export interface DebugTextDiff {
  identical: boolean;
  similarity: number;
  /** 'word' for short prose, 'line' for code and long output, 'none' when equal. */
  granularity: string;
  baseline_empty: boolean;
  hunks: { op: string; baseline: string[]; other: string[] }[];
}

/** A forked copy of one request, re-runnable without touching the original. */
export interface DebugSandbox {
  id: string;
  name: string;
  source_exchange_id: string;
  /** The real conversation it was forked from. Displayed only, never written. */
  source_chat_id: string;
  /** Its own conversation; forked memories and chat data live here. */
  sandbox_chat_id: string;
  message_list: { role: string; content: string }[];
  message_count: number;
  model: string;
  run_count: number;
  last_run_at: string;
  created_at: string;
}

export interface DebugComparison {
  baseline_id: string;
  /** Baseline first, so columns render straight from this. */
  order: string[];
  runs: Record<string, any>;
  diffs: Record<string, any>;
}

// One LLM parameter as the API carries it. The same shape at every scope that
// can name a model -- endpoint, per-model, rule, map stage, user settings --
// because the closest scope wins at request time.
export interface LlmParameter {
  name: string
  type: 'string' | 'string[]' | 'number' | 'float' | 'integer' | 'boolean'
  value: any
  description: string
  required: boolean
  options: string[]
}

export interface EndpointModel {
  id?: string
  name: string
  description?: string
  parameters?: LlmParameter[]
}

// Assistant Pane (Phase 26). The backend router (app/routers/assistant.py) is
// being written concurrently -- these shapes follow Planning/project-plan.md
// > "Phase 26 Design: Assistant Pane" > Architecture/Data model/Modules.
// Anything not spelled out exactly there (list-wrapper key names, save/fork
// return shapes, /catalog's shape) is a best-effort guess following this
// codebase's existing `{plural: [...]}` list convention -- see the 26c report.

export interface AssistantConfig {
  endpoint_id: string | null
  model: string
  parameters: LlmParameter[]
  context_tokens: number
  enabled: boolean
  endpoint_role_tag: string
  endpoint_name: string
}

export type AssistantMode = 'deny' | 'always_ask' | 'normal' | 'always_allow'
export type AssistantToolMode = AssistantMode | 'inherit'
export type AssistantRisk = 'read' | 'write' | 'destructive' | 'external_cost'

export interface AssistantToolPermission {
  name: string
  summary: string
  risk: AssistantRisk
  mode: AssistantToolMode
  effective: AssistantMode
}

export interface AssistantGroupPermission {
  key: string
  label: string
  page: string
  category: 'content' | 'configuration' | 'admin'
  default_mode: AssistantMode
  disabled_by_admin: boolean
  mode: AssistantMode | ''
  tools: AssistantToolPermission[]
}

export interface AssistantPermissions {
  groups: AssistantGroupPermission[]
}

export interface AssistantConversationSummary {
  id: string
  title: string
  saved: boolean
  updated_at: string
  prompt_tokens: number
  completion_tokens: number
  llm_calls: number
  tool_calls: number
}

/** Full conversation: `GET /conversations/{id}`. `messages` is OpenAI-format
 * (incl. `tool_calls` / `tool` roles), same shape stored in `messages_json`. */
export interface AssistantConversation {
  id: string
  title: string
  saved: boolean
  yolo: boolean
  messages: any[]
  compaction: { summary: string; through_index: number } | null
  pending: any | null
  prompt_tokens: number
  completion_tokens: number
  llm_calls: number
  tool_calls: number
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

  regenerateApiKey: () =>
    request<{ api_key: string }>('/api/auth/regenerate-key', { method: 'POST' }),

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
  createEndpoint: (data: { name: string; base_url: string; api_key: string; api_base_path?: string; enabled?: boolean; parameters?: LlmParameter[]; models?: EndpointModel[] }) =>
    request<any>('/api/endpoints', { method: 'POST', body: JSON.stringify(data) }),
  updateEndpoint: (id: string, data: any) =>
    request<any>(`/api/endpoints/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteEndpoint: (id: string) =>
    request<void>(`/api/endpoints/${id}`, { method: 'DELETE' }),
  listEndpointModels: (id: string) =>
    request<{ models: string[] }>(`/api/endpoints/${id}/models`),

  // Settings
  getSettings: () => request<{ default_endpoint_id: string | null; default_model: string; preserve_thinking: boolean; gitv_status: boolean; simulated_streaming_speed: number; parameters: LlmParameter[]; verification_parameters: LlmParameter[]; summarization_parameters: LlmParameter[] }>('/api/settings'),
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

  // Skills & Samples
  listSkills: () => request<{ skills: any[] }>('/api/skills'),
  createSkill: (data: { name: string; description?: string; content?: string; type: string }) =>
    request<any>('/api/skills', { method: 'POST', body: JSON.stringify(data) }),
  getSkill: (id: string) => request<any>(`/api/skills/${id}`),
  updateSkill: (id: string, data: any) =>
    request<any>(`/api/skills/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteSkill: (id: string) =>
    request<void>(`/api/skills/${id}`, { method: 'DELETE' }),
  attachSkill: (skillId: string, endpointId: string) =>
    request<any>(`/api/skills/${skillId}/attach`, { method: 'POST', body: JSON.stringify({ endpoint_id: endpointId }) }),
  detachSkill: (skillId: string, endpointId: string) =>
    request<void>(`/api/skills/${skillId}/attach/${endpointId}`, { method: 'DELETE' }),

  // Scenario Rules
  listScenarioRules: () => request<{ rules: any[] }>('/api/scenario-rules'),
  createScenarioRule: (data: any) =>
    request<any>('/api/scenario-rules', { method: 'POST', body: JSON.stringify(data) }),
  getScenarioRule: (id: string) => request<any>(`/api/scenario-rules/${id}`),
  updateScenarioRule: (id: string, data: any) =>
    request<any>(`/api/scenario-rules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteScenarioRule: (id: string) =>
    request<void>(`/api/scenario-rules/${id}`, { method: 'DELETE' }),
  getScenarioDefaultPrompt: () =>
    request<{ prompt: string }>('/api/scenario-rules/default-prompt'),

  // Verification
  listVerificationRules: () => request<{ rules: any[] }>('/api/verification/rules'),
  getVerificationRule: (id: string) => request<any>(`/api/verification/rules/${id}`),
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

  // Memory Rules
  listMemoryRules: () =>
    request<{ rules: any[] }>('/api/memory-rules'),
  getMemoryRule: (id: string) =>
    request<any>(`/api/memory-rules/${id}`),
  createMemoryRule: (data: any) =>
    request<any>('/api/memory-rules', { method: 'POST', body: JSON.stringify(data) }),
  updateMemoryRule: (id: string, data: any) =>
    request<any>(`/api/memory-rules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteMemoryRule: (id: string) =>
    request<void>(`/api/memory-rules/${id}`, { method: 'DELETE' }),

  // Maps
  listMaps: () =>
    request<{ maps: any[] }>('/api/maps'),
  getMap: (id: string) =>
    request<any>(`/api/maps/${id}`),
  createMap: (data: any) =>
    request<any>('/api/maps', { method: 'POST', body: JSON.stringify(data) }),
  updateMap: (id: string, data: any) =>
    request<any>(`/api/maps/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteMap: (id: string) =>
    request<void>(`/api/maps/${id}`, { method: 'DELETE' }),
  exportMap: (id: string, mode: 'embedded' | 'linked' = 'embedded') =>
    request<any>(`/api/maps/${id}/export?mode=${mode}`),
  importMap: (data: any, name?: string, resourceMode?: string) =>
    request<any>('/api/maps/import', {
      method: 'POST',
      body: JSON.stringify({ data, name, resource_mode: resourceMode }),
    }),

  // API Keys (Per-Endpoint)
  listApiKeys: () =>
    request<{ keys: any[] }>('/api/api-keys'),
  createApiKey: (data: any) =>
    request<any>('/api/api-keys', { method: 'POST', body: JSON.stringify(data) }),
  deleteApiKey: (id: string) =>
    request<void>(`/api/api-keys/${id}`, { method: 'DELETE' }),
  toggleApiKey: (id: string) =>
    request<any>(`/api/api-keys/${id}/toggle`, { method: 'PUT' }),

  // Audit Logs
  listAuditLogs: (limit = 100, offset = 0) =>
    request<{ logs: any[]; total: number }>(`/api/audit?limit=${limit}&offset=${offset}`),

  // Admin (admin only)
  getAdminSettings: () =>
    request<any>('/api/admin/settings'),
  updateAdminSettings: (data: any) =>
    request<any>('/api/admin/settings', { method: 'PUT', body: JSON.stringify(data) }),
  getAdminAuditLogs: (limit = 100, offset = 0) =>
    request<{ logs: any[]; total: number }>(`/api/admin/audit?limit=${limit}&offset=${offset}`),
  getSiteBanner: () =>
    request<{ banner: string; level: string }>('/api/site-banner'),
  runBackupNow: () =>
    request<any>('/api/admin/backup/run', { method: 'POST' }),
  listBackups: () =>
    request<any[]>('/api/admin/backup/list'),
  deleteBackup: (id: string) =>
    request<void>(`/api/admin/backup/${id}`, { method: 'DELETE' }),
  requestBackupRestore: (id: string) =>
    request<{ token: string }>(`/api/admin/backup/restore/${id}/request`, { method: 'POST' }),
  confirmBackupRestore: (id: string, token: string) =>
    request<any>(`/api/admin/backup/restore/${id}/confirm`, { method: 'POST', body: JSON.stringify({ token }) }),
  getServerLogs: (lines = 200) =>
    request<{ lines: string[]; total: number }>(`/api/admin/logs?lines=${lines}`),
  getSSLStatus: () =>
    request<{ cert_configured: boolean; cert_exists: boolean; cert_path: string | null; key_path: string | null; cert_info: any | null; is_active: boolean }>('/api/admin/ssl/status'),
  generateSSLCert: (extra_ips?: string[], extra_dns?: string[]) =>
    request<any>('/api/admin/ssl/generate', { method: 'POST', body: JSON.stringify({ extra_ips: extra_ips || null, extra_dns: extra_dns || null }) }),
  getCertIPCheck: () =>
    request<CertIPCheck>('/api/admin/ssl/ip-check'),
  acknowledgeCertIPCheck: (fingerprint: string) =>
    request<CertIPCheck>('/api/admin/ssl/ip-check/acknowledge', { method: 'POST', body: JSON.stringify({ fingerprint }) }),

  // Update management
  checkUpdate: () =>
    request<{ current_version: string; latest_version: string; update_available: boolean; release_url: string; release_notes: string;
    zip_url: string; step_count: number; error: string }>('/api/admin/update/check'),
  getUpdateDownloadInfo: () =>
    request<{ zip_url: string; current_version: string; latest_version: string; instructions: string }>('/api/admin/update/download-info'),
  executeUpdate: () =>
    request<{ success: boolean; message: string; error: string }>('/api/admin/update/execute', { method: 'POST' }),
  getUpdateChain: () =>
    request<{ active: boolean; status: string; from_version: string; target_version: string; current_step: number;
    total_steps: number; error: string;
    steps: { version: string; tag: string; status: string; attempts: number; error: string; release_url: string }[];
    log_tail: string[] }>('/api/admin/update/chain'),
  abortUpdateChain: () =>
    request<{ success: boolean; message: string; error: string }>('/api/admin/update/chain', { method: 'DELETE' }),
  resumeUpdateChain: () =>
    request<{ success: boolean; message: string; error: string }>('/api/admin/update/chain/resume', { method: 'POST' }),

  // Debug
  listDebugExchanges: (savedOnly = false) =>
    request<DebugListResponse>(`/api/debug${savedOnly ? '?saved_only=true' : ''}`),
  getDebugExchange: (id: string) =>
    request<DebugExchange>(`/api/debug/${id}`),
  clearDebugExchanges: (includeSaved = false) =>
    request<void>(`/api/debug${includeSaved ? '?include_saved=true' : ''}`, { method: 'DELETE' }),
  deleteDebugExchange: (id: string) =>
    request<void>(`/api/debug/${id}`, { method: 'DELETE' }),
  saveDebugExchange: (id: string, label = '') =>
    request<DebugExchange>(`/api/debug/${id}/save`, {
      method: 'POST',
      body: JSON.stringify({ label }),
    }),
  unsaveDebugExchange: (id: string) =>
    request<DebugExchange>(`/api/debug/${id}/save`, { method: 'DELETE' }),
  renameDebugExchange: (id: string, label: string) =>
    request<DebugExchange>(`/api/debug/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ label }),
    }),
  replayDebugExchange: (id: string) =>
    request<{ run_id: string; warning: string }>(`/api/debug/${id}/replay`, { method: 'POST' }),
  // Sandboxes: forked, re-runnable copies of a request.
  listDebugSandboxes: () =>
    request<{ sandboxes: DebugSandbox[] }>('/api/debug/sandboxes/list'),
  createDebugSandbox: (exchangeId: string, name = '') =>
    request<DebugSandbox>(`/api/debug/${exchangeId}/sandbox`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  runDebugSandbox: (sandboxId: string) =>
    request<{ run_id: string; run_count: number }>(
      `/api/debug/sandboxes/${sandboxId}/run`, { method: 'POST' },
    ),
  resetDebugSandbox: (sandboxId: string) =>
    request<DebugSandbox>(`/api/debug/sandboxes/${sandboxId}/reset`, { method: 'POST' }),
  updateDebugSandbox: (sandboxId: string, messages: any[]) =>
    request<DebugSandbox>(`/api/debug/sandboxes/${sandboxId}`, {
      method: 'PATCH',
      body: JSON.stringify({ messages }),
    }),
  deleteDebugSandbox: (sandboxId: string) =>
    request<void>(`/api/debug/sandboxes/${sandboxId}`, { method: 'DELETE' }),
  compareDebugExchanges: (ids: string[], baselineId = '') =>
    request<DebugComparison>('/api/debug/compare', {
      method: 'POST',
      body: JSON.stringify({ ids, baseline_id: baselineId }),
    }),

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

  // Content Packs
  listRepos: () => request<{ repos: any[]; disclaimer: string }>('/api/packs/repos'),
  linkRepo: (data: { name: string; url: string; branch?: string; token?: string }) =>
    request<any>('/api/packs/repos', { method: 'POST', body: JSON.stringify(data) }),
  syncRepo: (id: string) =>
    request<any>(`/api/packs/repos/${id}/sync`, { method: 'POST' }),
  browseRepo: (id: string) =>
    request<any>(`/api/packs/repos/${id}/browse`),
  deleteRepo: (id: string) =>
    request<void>(`/api/packs/repos/${id}`, { method: 'DELETE' }),
  checkUpdates: (id: string) =>
    request<any>(`/api/packs/repos/${id}/check-updates`, { method: 'POST' }),
  linkLocalRepo: (data: { name: string; path: string; is_global?: boolean }) =>
    request<any>('/api/packs/repos/local', { method: 'POST', body: JSON.stringify(data) }),
  createPack: (data: any) =>
    fetch('/api/packs/create', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${getToken()}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }).then(async r => {
      if (!r.ok) throw new Error((await r.json()).detail || 'Failed to create pack');
      return r.blob();
    }),
  installFile: (data: { repo_id: string; file_path: string; fork?: boolean }) =>
    request<any>('/api/packs/install', { method: 'POST', body: JSON.stringify(data) }),
  listInstalled: () =>
    request<{ items: any[]; disclaimer: string }>('/api/packs/installed'),
  toggleInstalled: (id: string) =>
    request<any>(`/api/packs/installed/${id}/toggle`, { method: 'PUT' }),
  uninstallItem: (id: string) =>
    request<void>(`/api/packs/installed/${id}`, { method: 'DELETE' }),

  // Tags - browse public tagged resources
  listPublicLorebooks: () => request<{ lorebooks: any[] }>('/api/lorebooks/public'),
  listPublicCantrips: () => request<{ cantrips: any[] }>('/api/cantrips/public'),

  // Assistant Pane
  getAssistantConfig: () => request<AssistantConfig>('/api/assistant/config'),
  updateAssistantConfig: (data: { endpoint_id?: string | null; model?: string; parameters?: LlmParameter[]; context_tokens?: number }) =>
    request<AssistantConfig>('/api/assistant/config', { method: 'PUT', body: JSON.stringify(data) }),
  getAssistantPermissions: () => request<AssistantPermissions>('/api/assistant/permissions'),
  updateAssistantPermissions: (data: { groups: Record<string, AssistantMode>; tools: Record<string, AssistantToolMode> }) =>
    request<AssistantPermissions>('/api/assistant/permissions', { method: 'PUT', body: JSON.stringify(data) }),
  // Shape beyond "compact catalog" is not pinned down anywhere in the design doc;
  // nothing in this sub-phase's UI consumes it yet.
  getAssistantCatalog: () => request<any>('/api/assistant/catalog'),
  listAssistantConversations: () =>
    request<{ conversations: AssistantConversationSummary[] }>('/api/assistant/conversations'),
  createAssistantConversation: () =>
    request<AssistantConversationSummary & { rotated_title: string | null }>(
      '/api/assistant/conversations', { method: 'POST' },
    ),
  getAssistantConversation: (id: string) =>
    request<AssistantConversation>(`/api/assistant/conversations/${id}`),
  patchAssistantConversation: (id: string, data: { title?: string; yolo?: boolean }) =>
    request<AssistantConversation>(`/api/assistant/conversations/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteAssistantConversation: (id: string) =>
    request<void>(`/api/assistant/conversations/${id}`, { method: 'DELETE' }),
  saveAssistantConversation: (id: string) =>
    request<AssistantConversation>(`/api/assistant/conversations/${id}/save`, { method: 'POST' }),
  unsaveAssistantConversation: (id: string) =>
    request<AssistantConversation>(`/api/assistant/conversations/${id}/save`, { method: 'DELETE' }),
  forkAssistantConversation: (id: string) =>
    request<AssistantConversationSummary & { rotated_title?: string | null }>(`/api/assistant/conversations/${id}/fork`, { method: 'POST', body: JSON.stringify({}) }),

  // Snapshots
  //
  // Deliberately generic. Six types keep version history and every one of them
  // uses the same three routes, so a per-type method set would be 24 wrappers
  // over one API.
  listSnapshots: (resourceType: string, resourceId: string) =>
    request<{ snapshots: any[] }>(
      `/api/snapshots?resource_type=${encodeURIComponent(resourceType)}&resource_id=${encodeURIComponent(resourceId)}`,
    ),
  getSnapshot: (id: string) => request<any>(`/api/snapshots/${id}`),
  createSnapshot: (resourceType: string, resourceId: string, label: string = '') =>
    request<any>('/api/snapshots', {
      method: 'POST',
      body: JSON.stringify({ resource_type: resourceType, resource_id: resourceId, label }),
    }),
  restoreSnapshotAsNew: (id: string, name: string = '') =>
    request<{ resource_type: string; resource_id: string; name: string; created: boolean; notes: string[] }>(
      `/api/snapshots/${id}/restore-as-new`,
      { method: 'POST', body: JSON.stringify({ name }) },
    ),
  restoreSnapshotInPlace: (id: string) =>
    request<{ resource_type: string; resource_id: string; name: string; created: boolean; notes: string[] }>(
      `/api/snapshots/${id}/restore-in-place`,
      { method: 'POST' },
    ),
  deleteSnapshot: (id: string) => request<void>(`/api/snapshots/${id}`, { method: 'DELETE' }),
  /** The live object a snapshot belongs to, for the history panel's diff. */
  getResourceForSnapshot: (resourceType: string, resourceId: string) => {
    const routes: Record<string, string> = {
      cantrip: '/api/cantrips',
      lorebook: '/api/lorebooks',
      skill: '/api/skills',
      sample: '/api/skills',
      verification_rule: '/api/verification/rules',
      memory_rule: '/api/memory-rules',
      scenario_rule: '/api/scenario-rules',
    }
    const base = routes[resourceType]
    if (!base) return Promise.resolve(null)
    return request<any>(`${base}/${resourceId}`).catch(() => null)
  },

  // Tag Groups
  listTagGroups: () => request<{ groups: any[] }>('/api/tag-groups'),
  createTagGroup: (data: { name: string; tag?: string; is_active?: boolean; members?: any[] }) =>
    request<any>('/api/tag-groups', { method: 'POST', body: JSON.stringify(data) }),
  getTagGroup: (id: string) => request<any>(`/api/tag-groups/${id}`),
  updateTagGroup: (id: string, data: { name?: string; tag?: string; is_active?: boolean }) =>
    request<any>(`/api/tag-groups/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteTagGroup: (id: string) => request<void>(`/api/tag-groups/${id}`, { method: 'DELETE' }),
  updateTagGroupMembers: (id: string, members: any[]) =>
    request<any>(`/api/tag-groups/${id}/members`, { method: 'PUT', body: JSON.stringify({ members }) }),
};
