import { isAuthenticated, isAdmin } from './stores';

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

export async function parseApiError(resp: Response): Promise<Error & { status?: number; detail?: unknown }> {
  const text = await resp.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
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
  const err = new Error(msg || resp.statusText) as Error & { status?: number; detail?: unknown };
  err.status = resp.status;
  err.detail = detail;
  return err;
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
    isAuthenticated.set(false);
    isAdmin.set(false);
    window.location.hash = '#/login';
    throw new Error('Unauthorized');
  }

  if (resp.status === 204) {
    return undefined as T;
  }

  if (!resp.ok) {
    throw await parseApiError(resp);
  }

  const text = await resp.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  return data as T;
}

function crud<TList = any>(basePath: string, opts: { exportable: true }): {
  list: () => Promise<TList>;
  get: (id: string) => Promise<any>;
  create: (data: any) => Promise<any>;
  update: (id: string, data: any) => Promise<any>;
  delete: (id: string) => Promise<void>;
  export: (id: string) => Promise<any>;
};
function crud<TList = any>(basePath: string, opts?: { exportable?: false }): {
  list: () => Promise<TList>;
  get: (id: string) => Promise<any>;
  create: (data: any) => Promise<any>;
  update: (id: string, data: any) => Promise<any>;
  delete: (id: string) => Promise<void>;
};
function crud<TList = any>(basePath: string, opts?: { exportable?: boolean }) {
  const base = {
    list: () => request<TList>(basePath),
    get: (id: string) => request<any>(`${basePath}/${id}`),
    create: (data: any) => request<any>(basePath, { method: 'POST', body: JSON.stringify(data) }),
    update: (id: string, data: any) => request<any>(`${basePath}/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
    delete: (id: string) => request<void>(`${basePath}/${id}`, { method: 'DELETE' }),
  };
  if (opts?.exportable) {
    return { ...base, export: (id: string) => request<any>(`${basePath}/${id}/export`) };
  }
  return base;
}

const cantripsCrud = crud<{ cantrips: any[] }>('/api/cantrips', { exportable: true });
const lorebooksCrud = crud<{ lorebooks: any[] }>('/api/lorebooks', { exportable: true });
const skillsCrud = crud<{ skills: any[] }>('/api/skills', { exportable: true });
const scenarioRulesCrud = crud<{ rules: any[] }>('/api/scenario-rules');
const verificationRulesCrud = crud<{ rules: VerificationRule[] }>('/api/verification/rules', { exportable: true });
const memoryRulesCrud = crud<{ rules: any[] }>('/api/memory-rules', { exportable: true });
const mapsCrud = crud<{ maps: MapResponse[] }>('/api/maps', { exportable: true });
const tagGroupsCrud = crud<{ groups: any[] }>('/api/tag-groups');

/** Retry helper for the safety-scanner blocking gate (HTTP 422).
 *
 * The backend refuses a write whose scan produced critical findings until the
 * client re-sends it with `override_scan: true`. This helper surfaces the
 * findings, asks the user, and re-sends once with the override set.
 */
export function withScanOverride(attempt: (overrideScan: boolean) => Promise<any>): Promise<any> {
  return attempt(false).catch((err: any) => {
    const findings: unknown[] | undefined = err?.detail?.scan_findings;
    if (!Array.isArray(findings) || findings.length === 0) throw err;
    const lines = findings.map(
      (f: any) => `[${f.severity}] ${f.description}${f.line ? ` (line ${f.line})` : ''}`,
    );
    const ok = confirm(
      `The safety scanner blocked this save:\n\n${lines.join('\n')}\n\n` +
        'These patterns can be harmless in ordinary content, but they are also what ' +
        'sandbox-escape attempts look like. Save anyway?',
    );
    if (!ok) throw err;
    return attempt(true);
  });
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
  cached_tokens?: number | null;
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

/** Metrics (0.31.0): one prompt-mutating object aggregated over a window. */
export interface PromptObjectAggregate {
  object_type: string;
  object_id: string;
  parent_id: string;
  object_name: string;
  parent_name: string;
  fires: number;
  sum_difference: number;
  avg_difference: number;
}

export interface PromptTypeTotal {
  object_type: string;
  fires: number;
  sum_difference: number;
}

/** One purpose/endpoint/model group of LLM calls over a window. */
export interface LlmCallAggregate {
  purpose: string;
  endpoint_id: string;
  endpoint_name: string;
  model: string;
  rule_id: string;
  rule_name: string;
  calls: number;
  ok: number;
  errors: number;
  retries: number;
  failovers: number;
  mean_latency_ms: number;
  median_latency_ms: number;
  min_latency_ms: number;
  max_latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  cached_tokens?: number | null;
  tokens_source_counts: { usage: number; estimate: number };
}

export interface MetricsSummary {
  window_days: number;
  chat_id: string;
  include_flagged: boolean;
  chats: string[];
  prompt_objects: PromptObjectAggregate[];
  prompt_type_totals: PromptTypeTotal[];
  llm_calls: LlmCallAggregate[];
}

export interface PromptObjectMetricRow {
  id: string;
  chat_id: string;
  context: string;
  is_retry: boolean;
  created_at: string;
  object_type: string;
  object_id: string;
  parent_id: string;
  pre_tokens: number;
  post_tokens: number;
  difference: number;
}

export interface LlmCallMetricRow {
  id: string;
  chat_id: string;
  context: string;
  is_retry: boolean;
  created_at: string;
  purpose: string;
  endpoint_id: string;
  endpoint_name: string;
  model: string;
  status_code: number | null;
  latency_ms: number;
  failover_attempt: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  reasoning_tokens: number | null;
  cached_tokens?: number | null;
  tokens_source: string;
  rule_id: string;
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
  cached_tokens?: number | null;
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

export interface CacheAnalysis {
  available: boolean;
  reason?: string;
  previous_exchange_id?: string;
  shared_prefix_tokens?: number;
  total_prompt_tokens?: number;
  prefix_stability_pct?: number;
  first_diff_message_index?: number | null;
  first_diff_role?: string;
  culprit_stage?: string;
  is_tail_only?: boolean;
  summary?: string;
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
  cache_analysis?: CacheAnalysis | null;
  created_at: string;
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

export interface VerificationRule {
  id: string
  name: string
  description?: string
  prompt: string
  regex_pattern?: string
  is_active: boolean
  max_retries: number
  execution_order: number
  resubmission_strategy: string
  verification_endpoint_id?: string | null
  verification_model?: string
  parameters?: LlmParameter[]
  tag?: string
  [key: string]: any
}

export type VerificationRuleCreate = Partial<VerificationRule> & { name: string; prompt: string }
export type VerificationRuleUpdate = Partial<VerificationRule>

export interface MapStage {
  name?: string
  description?: string
  system_instructions?: string
  endpoint_id?: string | null
  endpoint_tag?: string
  model_override?: string
  driver_callable_turns?: number
  verification_enabled?: boolean
  verification_endpoint_id?: string | null
  verification_model?: string
  verification_max_retries?: number
  verification_instructions?: string
  output_mode?: string
  base_prompt_mode?: string
  history_window?: number
  condition_json?: string
  parameters?: LlmParameter[]
  verification_parameters?: LlmParameter[]
  resources?: any[]
  [key: string]: any
}

export interface MapResponse {
  id: string
  name: string
  description?: string
  tag?: string
  is_public?: boolean
  version?: string
  author?: string
  global_llm_instructions?: string
  final_verification_enabled?: boolean
  stages: MapStage[]
  [key: string]: any
}

export type MapCreate = Partial<MapResponse> & { name: string; stages: MapStage[] }
export type MapUpdate = Partial<MapResponse>

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

export interface Cantrip {
  id: string
  name: string
  description?: string
  llm_instructions?: string
  code?: string
  hook_type?: string
  is_active?: boolean
  is_public?: boolean
  run_pre_driver?: boolean
  run_driver_callable?: boolean
  run_pre_navigator?: boolean
  run_post_navigator?: boolean
  execution_order?: number
  timeout_ms?: number
  budget_weight?: number
  reserved_tokens?: number
  lorebook_reads?: string[]
  user_data_prefixes?: string[]
  response_tags?: string[]
  tag?: string
  [key: string]: any
}

export type CantripCreate = Partial<Cantrip> & { name: string }
export type CantripUpdate = Partial<Cantrip>

export interface CantripTestResponse {
  error?: string | null
  scenario?: string
  personality?: string
  debug_logs?: string[]
  chat_data?: Record<string, any>
  tokens_before?: number
  tokens_after?: number
  token_delta?: number
  [key: string]: any
}

export interface SequenceTurnResult {
  index: number
  tags?: string[]
  is_regenerate?: boolean
  cantrips?: Record<string, any[]>
  response_tags?: Record<string, any>
  reply?: string
  chat_data?: Record<string, any>
  tokens_before?: number
  tokens_after?: number
  token_delta?: number
  [key: string]: any
}

export interface SequenceTestResponse {
  turns: SequenceTurnResult[]
  stopped_at_turn?: number | null
  stop_reason?: string | null
  final_chat_data?: Record<string, any>
  final_user_data?: Record<string, any>
  final_cantrip_data?: Record<string, any>
  total_token_delta?: number
  [key: string]: any
}

export interface Lorebook {
  id: string
  name: string
  description?: string
  is_public?: boolean
  budget_weight?: number
  reserved_tokens?: number
  inject_after_cantrips?: boolean
  scan_depth?: number
  word_boundary_keys?: boolean
  tag?: string
  [key: string]: any
}

export type LorebookCreate = Partial<Lorebook> & { name: string }
export type LorebookUpdate = Partial<Lorebook>

export interface Skill {
  id: string
  name: string
  description?: string
  content?: string
  type: string
  budget_weight?: number
  reserved_tokens?: number
  endpoints?: string[]
  [key: string]: any
}

export type SkillCreate = Partial<Skill> & { name: string; type: string }
export type SkillUpdate = Partial<Skill>

export interface UserSettings {
  default_endpoint_id: string | null;
  default_model: string;
  preserve_thinking: boolean;
  gitv_status: boolean;
  simulated_streaming_speed: number;
  streaming_post_processing_enabled: boolean;
  lexicon_enabled: boolean;
  lexicon_endpoint_id: string | null;
  cantrip_auto_install_enabled: boolean;
  passenger_calling_enabled: boolean;
  passenger_call_budget: number;
  passenger_effective_cap: number;
  response_strip_patterns: string[];
  parameters: LlmParameter[];
  verification_parameters: LlmParameter[];
  summarization_parameters: LlmParameter[];
  lexicon_parameters: LlmParameter[];
  passenger_parameters: LlmParameter[];
  summarization_block_size?: number;
  cache_friendly_mode?: boolean;
  auto_cache_control?: boolean;
  [key: string]: any;
}

export interface DenoPoolStatus {
  idle_count: number;
  min_idle: number;
  max_idle: number;
  idle_ttl_seconds: number;
  total_warm_hits: number;
  total_cold_spawns: number;
}

export interface AdminSettings {
  max_driver_callable_turns: number;
  max_verification_retries: number;
  max_map_stages: number;
  rate_limit_proxy_per_min: number;
  rate_limit_api_per_min: number;
  max_memory_size_mb: number;
  max_script_size_kb: number;
  max_rule_size_kb: number;
  max_lorebook_size_kb: number;
  max_saved_debug_runs: number;
  max_debug_exchange_kb: number;
  url_blocklist: string;
  runtime_log_level: string;
  effective_log_level: string;
  site_banner: string;
  site_banner_level: string;
  backup_schedule_enabled: boolean;
  backup_schedule_days: string;
  backup_schedule_time: string;
  backup_retention_count: number;
  backup_dir: string;
  assistant_enabled: boolean;
  assistant_admin_reads_enabled: boolean;
  assistant_packs_enabled: boolean;
  max_assistant_tool_calls_per_turn: number;
  max_assistant_conversations: number;
  max_assistant_tool_result_kb: number;
  max_snapshots_per_object: number;
  metrics_retention_days: number;
  assistant_cantrip_auto_install_allowed?: boolean;
  passenger_calling_enabled?: boolean;
  max_passenger_calls_per_request?: number;
  passenger_timeout_seconds?: number;
  deno_pool_min_idle?: number;
  deno_pool_max_idle?: number;
  deno_pool_idle_ttl_seconds?: number;
  deno_pool_status?: DenoPoolStatus | null;
  [key: string]: any;
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
  /** The only call that returns the stored provider key; every reveal is audited server-side. */
  revealEndpointKey: (id: string) =>
    request<{ api_key: string }>(`/api/endpoints/${id}/reveal`, { method: 'POST' }),
  createEndpoint: (data: { name: string; base_url: string; api_key: string; api_base_path?: string; enabled?: boolean; parameters?: LlmParameter[]; models?: EndpointModel[] }) =>
    request<any>('/api/endpoints', { method: 'POST', body: JSON.stringify(data) }),
  updateEndpoint: (id: string, data: any) =>
    request<any>(`/api/endpoints/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteEndpoint: (id: string) =>
    request<void>(`/api/endpoints/${id}`, { method: 'DELETE' }),
  listEndpointModels: (id: string) =>
    request<{ models: string[] }>(`/api/endpoints/${id}/models`),

  // Settings
  getSettings: () => request<UserSettings>('/api/settings'),
  updateSettings: (data: any) =>
    request<any>('/api/settings', { method: 'PUT', body: JSON.stringify(data) }),

  // Cantrips
  listCantrips: () => cantripsCrud.list() as Promise<{ cantrips: Cantrip[] }>,
  createCantrip: (data: CantripCreate) => cantripsCrud.create(data) as Promise<Cantrip>,
  getCantrip: (id: string) => cantripsCrud.get(id) as Promise<Cantrip>,
  updateCantrip: (id: string, data: CantripUpdate) => cantripsCrud.update(id, data) as Promise<Cantrip>,
  deleteCantrip: cantripsCrud.delete,
  exportCantrip: cantripsCrud.export,
  testCantripById: (id: string, data: any) =>
    request<CantripTestResponse>(`/api/cantrips/${id}/test`, { method: 'POST', body: JSON.stringify(data) }),
  testCantripSequence: (data: any) =>
    request<SequenceTestResponse>('/api/cantrips/test-sequence', { method: 'POST', body: JSON.stringify(data) }),

  // Storage browser
  listStorageChats: (search = '', limit = 100, offset = 0) =>
    request<{ conversations: any[]; total: number }>(`/api/storage/chats?search=${encodeURIComponent(search)}&limit=${limit}&offset=${offset}`),
  listStorageRows: (scope: 'chat-data' | 'user-data' | 'cantrip-data', params: { conversation_id?: string; cantrip_id?: string; search?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.conversation_id) q.set('conversation_id', params.conversation_id)
    if (params.cantrip_id) q.set('cantrip_id', params.cantrip_id)
    if (params.search) q.set('search', params.search)
    q.set('limit', String(params.limit ?? 100))
    q.set('offset', String(params.offset ?? 0))
    return request<{ rows: any[]; total: number }>(`/api/storage/${scope}?${q.toString()}`)
  },
  getStorageRow: (scope: 'chat-data' | 'user-data' | 'cantrip-data', id: string) =>
    request<any>(`/api/storage/${scope}/${id}`),
  putStorageRow: (scope: 'chat-data' | 'user-data' | 'cantrip-data', id: string, data: { key?: string; value: string }) =>
    request<any>(`/api/storage/${scope}/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteStorageRow: (scope: 'chat-data' | 'user-data' | 'cantrip-data', id: string) =>
    request<void>(`/api/storage/${scope}/${id}`, { method: 'DELETE' }),
  copyStorageRow: (id: string, targetConversationId: string) =>
    request<any>(`/api/storage/chat-data/${id}/copy`, { method: 'POST', body: JSON.stringify({ target_conversation_id: targetConversationId }) }),
  clearStorageConversation: (conversationId: string) =>
    request<void>(`/api/storage/chats/${encodeURIComponent(conversationId)}`, { method: 'DELETE' }),
  copyStorageConversation: (conversationId: string, target: string) =>
    request<{ copied: number; skipped: string[] }>(`/api/storage/chats/${encodeURIComponent(conversationId)}/copy`, { method: 'POST', body: JSON.stringify({ target_conversation_id: target }) }),
  exportStorage: (scope: 'chats' | 'chat' | 'user' | 'cantrip' | 'all', conversationId = '', cantripId = '') => {
    const q = new URLSearchParams({ scope })
    if (conversationId) q.set('conversation_id', conversationId)
    if (cantripId) q.set('cantrip_id', cantripId)
    return request<any>(`/api/storage/export?${q.toString()}`)
  },
  importStorage: (payload: any, targetConversationId = '', targetCantripId = '') =>
    request<{ imported: number; skipped: string[] }>('/api/storage/import', { method: 'POST', body: JSON.stringify({ payload, target_conversation_id: targetConversationId, target_cantrip_id: targetCantripId }) }),
  validateCantrip: (code: string) =>
    request<{ valid: boolean; error: string | null }>('/api/cantrips/validate', { method: 'POST', body: JSON.stringify({ code }) }),
  listTemplates: () =>
    request<{ templates: any[] }>('/api/cantrips/templates'),
  installTemplate: (name: string) =>
    request<any>('/api/cantrips/templates/install', { method: 'POST', body: JSON.stringify({ template_name: name }) }),

  // Diagnostics
  runAudit: (endpointId?: string, model?: string) => {
    const q = new URLSearchParams()
    if (endpointId) q.set('endpoint_id', endpointId)
    if (model) q.set('model', model)
    const qs = q.toString()
    return request<{ results: any[]; all_passed: boolean }>(`/api/diagnostics/audit${qs ? `?${qs}` : ''}`)
  },

  // Lorebooks
  listLorebooks: () => lorebooksCrud.list() as Promise<{ lorebooks: Lorebook[] }>,
  createLorebook: (data: LorebookCreate) => lorebooksCrud.create(data) as Promise<Lorebook>,
  getLorebook: (id: string) => lorebooksCrud.get(id) as Promise<Lorebook>,
  updateLorebook: (id: string, data: LorebookUpdate) => lorebooksCrud.update(id, data) as Promise<Lorebook>,
  deleteLorebook: lorebooksCrud.delete,
  addLorebookEntry: (lbId: string, data: any) =>
    request<any>(`/api/lorebooks/${lbId}/entries`, { method: 'POST', body: JSON.stringify(data) }),
  updateLorebookEntry: (lbId: string, entryId: string, data: any) =>
    request<any>(`/api/lorebooks/${lbId}/entries/${entryId}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteLorebookEntry: (lbId: string, entryId: string) =>
    request<void>(`/api/lorebooks/${lbId}/entries/${entryId}`, { method: 'DELETE' }),
  importLorebook: (data: any) =>
    request<any>('/api/lorebooks/import', { method: 'POST', body: JSON.stringify(data) }),
  exportLorebook: lorebooksCrud.export,

  // Skills & Samples
  listSkills: () => skillsCrud.list() as Promise<{ skills: Skill[] }>,
  createSkill: (data: SkillCreate) =>
    skillsCrud.create(data) as Promise<Skill>,
  getSkill: (id: string) => skillsCrud.get(id) as Promise<Skill>,
  updateSkill: (id: string, data: SkillUpdate) => skillsCrud.update(id, data) as Promise<Skill>,
  deleteSkill: skillsCrud.delete,
  attachSkill: (skillId: string, endpointId: string) =>
    request<any>(`/api/skills/${skillId}/attach`, { method: 'POST', body: JSON.stringify({ endpoint_id: endpointId }) }),
  detachSkill: (skillId: string, endpointId: string) =>
    request<void>(`/api/skills/${skillId}/attach/${endpointId}`, { method: 'DELETE' }),
  exportSkill: skillsCrud.export,

  // Scenario Rules
  listScenarioRules: scenarioRulesCrud.list,
  createScenarioRule: scenarioRulesCrud.create,
  getScenarioRule: scenarioRulesCrud.get,
  updateScenarioRule: scenarioRulesCrud.update,
  deleteScenarioRule: scenarioRulesCrud.delete,
  getScenarioDefaultPrompt: () =>
    request<{ prompt: string }>('/api/scenario-rules/default-prompt'),

  // Verification
  listVerificationRules: verificationRulesCrud.list,
  getVerificationRule: (id: string) => verificationRulesCrud.get(id) as Promise<VerificationRule>,
  createVerificationRule: (data: VerificationRuleCreate) => verificationRulesCrud.create(data) as Promise<VerificationRule>,
  updateVerificationRule: (id: string, data: VerificationRuleUpdate) => verificationRulesCrud.update(id, data) as Promise<VerificationRule>,
  deleteVerificationRule: verificationRulesCrud.delete,
  exportVerificationRule: verificationRulesCrud.export,
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
  listMemoryRules: memoryRulesCrud.list,
  getMemoryRule: memoryRulesCrud.get,
  createMemoryRule: memoryRulesCrud.create,
  updateMemoryRule: memoryRulesCrud.update,
  deleteMemoryRule: memoryRulesCrud.delete,
  exportMemoryRule: memoryRulesCrud.export,

  // Maps
  listMaps: mapsCrud.list,
  getMap: (id: string) => mapsCrud.get(id) as Promise<MapResponse>,
  createMap: (data: MapCreate) => mapsCrud.create(data) as Promise<MapResponse>,
  updateMap: (id: string, data: MapUpdate) => mapsCrud.update(id, data) as Promise<MapResponse>,
  deleteMap: mapsCrud.delete,
  exportMap: (id: string, mode: 'embedded' | 'linked' = 'embedded') =>
    request<any>(`/api/maps/${id}/export?mode=${mode}`),
  importMap: (data: any, name?: string, resourceMode?: string, overrideScan: boolean = false) =>
    request<any>('/api/maps/import', {
      method: 'POST',
      body: JSON.stringify({ data, name, resource_mode: resourceMode, override_scan: overrideScan }),
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

  // Metrics
  getMetricsSummary: (days = 30, chatId = '', includeFlagged = false) =>
    request<MetricsSummary>(`/api/metrics/summary?days=${days}`
      + `${chatId ? `&chat_id=${encodeURIComponent(chatId)}` : ''}`
      + `${includeFlagged ? '&include_flagged=true' : ''}`),
  listPromptObjectMetrics: (limit = 100, offset = 0, chatId = '') =>
    request<{ rows: PromptObjectMetricRow[]; total: number }>(`/api/metrics/prompt-objects?limit=${limit}&offset=${offset}`
      + `${chatId ? `&chat_id=${encodeURIComponent(chatId)}` : ''}`),
  listLlmCallMetrics: (limit = 100, offset = 0, chatId = '') =>
    request<{ rows: LlmCallMetricRow[]; total: number }>(`/api/metrics/llm-calls?limit=${limit}&offset=${offset}`
      + `${chatId ? `&chat_id=${encodeURIComponent(chatId)}` : ''}`),

  // Admin (admin only)
  getAdminSettings: () =>
    request<AdminSettings>('/api/admin/settings'),
  updateAdminSettings: (data: Partial<AdminSettings>) =>
    request<AdminSettings>('/api/admin/settings', { method: 'PUT', body: JSON.stringify(data) }),
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
  executeUpdate: (options?: { auto_approve?: boolean }) =>
    request<{ success: boolean; message: string; error: string }>('/api/admin/update/execute', {
      method: 'POST',
      body: JSON.stringify(options ?? { auto_approve: true }),
    }),
  getUpdateChain: () =>
    request<{ active: boolean; status: string; auto_approve?: boolean; from_version: string; target_version: string; current_step: number;
    total_steps: number; error: string;
    steps: { version: string; tag: string; status: string; attempts: number; error: string; release_url: string }[];
    log_tail: string[] }>('/api/admin/update/chain'),
  abortUpdateChain: () =>
    request<{ success: boolean; message: string; error: string }>('/api/admin/update/chain', { method: 'DELETE' }),
  resumeUpdateChain: (options?: { auto_approve?: boolean }) =>
    request<{ success: boolean; message: string; error: string }>('/api/admin/update/chain/resume', {
      method: 'POST',
      body: JSON.stringify(options ?? {}),
    }),

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
      summarization_block_size?: number;
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

  // Assistant Pane
  getAssistantConfig: () => request<AssistantConfig>('/api/assistant/config'),
  updateAssistantConfig: (data: { endpoint_id?: string | null; model?: string; parameters?: LlmParameter[]; context_tokens?: number }) =>
    request<AssistantConfig>('/api/assistant/config', { method: 'PUT', body: JSON.stringify(data) }),
  getAssistantPermissions: () => request<AssistantPermissions>('/api/assistant/permissions'),
  updateAssistantPermissions: (data: { groups: Record<string, AssistantMode>; tools: Record<string, AssistantToolMode> }) =>
    request<AssistantPermissions>('/api/assistant/permissions', { method: 'PUT', body: JSON.stringify(data) }),
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
  // Phase 29b: cantrip drafts queued from chat via <CANTRIP:description>.
  listAssistantDrafts: () =>
    request<{ drafts: any[] }>('/api/assistant/drafts'),
  patchAssistantDraft: (id: string, status: string) =>
    request<any>(`/api/assistant/drafts/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) }),

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
  restoreSnapshotAsNew: (id: string, name: string = '', overrideScan: boolean = false) =>
    request<{ resource_type: string; resource_id: string; name: string; created: boolean; notes: string[] }>(
      `/api/snapshots/${id}/restore-as-new`,
      { method: 'POST', body: JSON.stringify({ name, override_scan: overrideScan }) },
    ),
  restoreSnapshotInPlace: (id: string, overrideScan: boolean = false) =>
    request<{ resource_type: string; resource_id: string; name: string; created: boolean; notes: string[] }>(
      `/api/snapshots/${id}/restore-in-place`,
      { method: 'POST', body: JSON.stringify({ override_scan: overrideScan }) },
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
      map: '/api/maps',
    }
    const base = routes[resourceType]
    if (!base) return Promise.resolve(null)
    return request<any>(`${base}/${resourceId}`).catch(() => null)
  },

  // Tag Groups
  listTagGroups: tagGroupsCrud.list,
  createTagGroup: (data: { name: string; tag?: string; is_active?: boolean; activate_members?: boolean; members?: any[] }) =>
    tagGroupsCrud.create(data),
  getTagGroup: tagGroupsCrud.get,
  updateTagGroup: (id: string, data: { name?: string; tag?: string; is_active?: boolean; activate_members?: boolean }) =>
    tagGroupsCrud.update(id, data),
  deleteTagGroup: tagGroupsCrud.delete,
  updateTagGroupMembers: (id: string, members: any[]) =>
    request<any>(`/api/tag-groups/${id}/members`, { method: 'PUT', body: JSON.stringify({ members }) }),
};
