<script lang="ts">
  import { api, getApiKey, setApiKey } from '../api'
  import { onMount } from 'svelte'

  let settings = {
    default_endpoint_id: '' as string | null,
    default_model: '',
    preserve_thinking: true,
    gitv_status: false,
    simulated_streaming_speed: 0,
  }
  let summarization = {
    summarization_enabled: false,
    summarization_endpoint_id: '' as string | null,
    summarization_model: '',
    summarization_token_threshold: 8000,
    summarization_keep_recent: 6,
    summarization_prompt: '',
  }
  let driverCallableTurns = 1
  let bypassMethod = 'none'
  let prefillEnabled = false
  let budgetPercent = 10.0
  let contextWindowOverride = 0
  let debugMode = false
  let endpoints: any[] = []
  let apiKey: string | null = null
  let error = ''
  let saved = false
  let sumSaved = false
  let showKey = false
  let copied = false
  let regenerating = false
  let newKeyNotice = false

  async function load() {
    try {
      const [s, e, sum] = await Promise.all([api.getSettings(), api.listEndpoints(), api.getSummarizationSettings()])
      settings.default_endpoint_id = s.default_endpoint_id || ''
      settings.default_model = s.default_model || ''
      settings.preserve_thinking = s.preserve_thinking
      settings.gitv_status = s.gitv_status
      settings.simulated_streaming_speed = s.simulated_streaming_speed || 0
      endpoints = e.endpoints
      apiKey = getApiKey()
      summarization.summarization_enabled = sum.summarization_enabled
      summarization.summarization_endpoint_id = sum.summarization_endpoint_id || ''
      summarization.summarization_model = sum.summarization_model || ''
      summarization.summarization_token_threshold = sum.summarization_token_threshold || 8000
      summarization.summarization_keep_recent = sum.summarization_keep_recent ?? 6
      summarization.summarization_prompt = sum.summarization_prompt || ''

      try {
        const me = await api.getMe()
        if (me.is_admin || true) {
          const s = await api.getSettings()
          driverCallableTurns = (s as any).driver_callable_turns ?? 1
          bypassMethod = (s as any).bypass_method ?? 'none'
          prefillEnabled = (s as any).prefill_enabled ?? false
          budgetPercent = (s as any).context_budget_percent ?? 10.0
          contextWindowOverride = (s as any).context_window_override ?? 0
          debugMode = (s as any).debug_mode ?? false
        }
      } catch {}
    } catch (e: any) { error = e.message }
  }

  async function save() {
    error = ''; saved = false
    try { await api.updateSettings(settings); saved = true }
    catch (e: any) { error = e.message }
  }

  async function saveSummarization() {
    error = ''; sumSaved = false
    try {
      await api.updateSummarizationSettings({
        ...summarization,
        summarization_endpoint_id: summarization.summarization_endpoint_id || '',
      })
      sumSaved = true
    } catch (e: any) { error = e.message }
  }

  async function saveDriverCallable() {
    error = ''
    try {
      await api.updateSettings({ driver_callable_turns: driverCallableTurns } as any)
    } catch (e: any) { error = e.message }
  }

  async function saveBypassPrefill() {
    error = ''
    try {
      await api.updateSettings({ bypass_method: bypassMethod, prefill_enabled: prefillEnabled } as any)
    } catch (e: any) { error = e.message }
  }

  async function saveBudget() {
    error = ''
    try {
      await api.updateSettings({ context_budget_percent: budgetPercent, context_window_override: contextWindowOverride, debug_mode: debugMode } as any)
    } catch (e: any) { error = e.message }
  }

  function copyKey() {
    if (!apiKey) return
    navigator.clipboard.writeText(apiKey)
    copied = true
    setTimeout(() => copied = false, 2000)
  }

  async function regenerateKey() {
    if (!confirm('Regenerate your API key? The old key will stop working immediately. The new key will be shown once.')) return
    error = ''; regenerating = true
    try {
      const data = await api.regenerateApiKey()
      apiKey = data.api_key
      setApiKey(data.api_key)
      newKeyNotice = true
    } catch (e: any) { error = e.message }
    finally { regenerating = false }
  }

  onMount(load)
</script>

<div class="page-header"><h2>Settings</h2></div>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if saved}<div class="success-msg">Settings saved.</div>{/if}

<div class="card">
  <h3>Proxy Configuration</h3>
  <div class="form-group">
    <label for="default-ep">Default Endpoint</label>
    <select id="default-ep" bind:value={settings.default_endpoint_id}>
      <option value="">None</option>
      {#each endpoints as ep}<option value={ep.id}>{ep.name}</option>{/each}
    </select>
  </div>
  <div class="form-group">
    <label for="default-model">Default Model Override</label>
    <input id="default-model" autocomplete="off" bind:value={settings.default_model} placeholder="Leave blank to use client's model" />
  </div>
  <button class="primary" onclick={save}>Save Settings</button>
</div>

<div class="card">
  <h3>Streaming and Status</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    These settings affect how responses are delivered when verification is enabled. Since verification requires buffering the full response, these options let you control the streaming experience.
  </p>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={settings.gitv_status} style="width: auto;">
      GITV Status Block
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Includes a &lt;think&gt;&lt;gitv&gt; status block before the response showing pipeline activity (verification results, etc).
    </p>
  </div>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={settings.preserve_thinking} style="width: auto;">
      Preserve Thinking
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Include the LLM's reasoning/thought process in the final response if the model produces it.
    </p>
  </div>
  <div class="form-group">
    <label for="stream-speed">Simulated Streaming Speed (tokens/min, 0 = instant)</label>
    <input id="stream-speed" type="number" bind:value={settings.simulated_streaming_speed} placeholder="0" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      When verification buffers the response, simulate streaming output at this speed. Set to 0 to deliver the entire response at once.
    </p>
  </div>
  <button class="primary" onclick={save}>Save Settings</button>
</div>

<div class="card">
  <h3>Conversation Summarization</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    Automatically compress older conversation history into a summary when the token count exceeds the threshold. The summary is injected as context so the model retains long-running conversation details without filling the context window. Triggered before forwarding; works for both streaming and non-streaming requests.
  </p>
  {#if sumSaved}<div class="success-msg">Summarization settings saved.</div>{/if}
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={summarization.summarization_enabled} style="width: auto;">
      Enable Summarization
    </label>
  </div>
  <div class="form-group">
    <label for="sum-ep">Summarization Endpoint</label>
    <select id="sum-ep" bind:value={summarization.summarization_endpoint_id}>
      <option value="">Use default endpoint</option>
      {#each endpoints as ep}<option value={ep.id}>{ep.name}</option>{/each}
    </select>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Endpoint used to generate summaries. Can be a cheaper/faster model than the main endpoint.
    </p>
  </div>
  <div class="form-group">
    <label for="sum-model">Summarization Model Override</label>
    <input id="sum-model" autocomplete="off" bind:value={summarization.summarization_model} placeholder="Leave blank to use endpoint default" />
  </div>
  <div class="form-group">
    <label for="sum-threshold">Token Threshold</label>
    <input id="sum-threshold" type="number" min="1" bind:value={summarization.summarization_token_threshold} placeholder="8000" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      When the estimated token count of the conversation exceeds this value, older messages are summarized. Token count is estimated at roughly 4 characters per token.
    </p>
  </div>
  <div class="form-group">
    <label for="sum-keep">Keep Recent Messages</label>
    <input id="sum-keep" type="number" min="3" bind:value={summarization.summarization_keep_recent} placeholder="6" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Number of recent messages always sent verbatim (never summarized). Minimum 3 to ensure a clean separation between the summary and current context. The rest are compressed into a summary.
    </p>
  </div>
  <div class="form-group">
    <label for="sum-prompt">Summarization Prompt</label>
    <textarea id="sum-prompt" bind:value={summarization.summarization_prompt} rows="4" placeholder="Custom instructions for the summarization model. Leave blank to use the default."></textarea>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      System prompt sent to the summarization model. If cleared on save, the default prompt is restored on reload.
    </p>
  </div>
  <button class="primary" onclick={saveSummarization}>Save Summarization Settings</button>
</div>

<div class="card">
  <h3>Driver-Callable Tools</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    When cantrips or lorebooks with the Driver-Callable position are active, the writing LLM can invoke them as tools via call tags. This setting controls the maximum number of tool-call rounds per request. Set to 0 to disable tool access entirely.
  </p>
  <div class="form-group">
    <label for="dc-turns">Driver-Callable Turns (0 = disabled)</label>
    <input id="dc-turns" type="number" min="0" bind:value={driverCallableTurns} placeholder="1" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Auto-disables when no Driver-Callable resources are active for a request, regardless of this setting.
    </p>
  </div>
  <button class="primary" onclick={saveDriverCallable}>Save</button>
</div>

<div class="card">
  <h3>Prefill Normalization</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    When a trailing assistant message is detected (prefill pattern), convert it to a system instruction for OpenAI-compatible providers that don't support native prefill. Anthropic and Google endpoints pass through as-is.
  </p>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={prefillEnabled} style="width: auto;">
      Enable Prefill Normalization
    </label>
  </div>
  <button class="primary" onclick={saveBypassPrefill}>Save</button>
</div>

<div class="card">
  <h3>Content Bypass</h3>
  <div class="error-msg" style="margin-bottom: 12px; font-size: 11px;">
    WARNING: Content bypass plugins modify requests to work around provider content filters. These actions may violate your service provider's Terms of Service. Use at your own risk.
  </div>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    Encodes outgoing user messages to reduce detectability by keyword-based content filters. Response content is decoded before returning to the client.
  </p>
  <div class="form-group">
    <label for="bypass-method">Bypass Method</label>
    <select id="bypass-method" bind:value={bypassMethod}>
      <option value="none">None (disabled)</option>
      <option value="space_separation">Space Separation (zero-width spaces in sensitive words)</option>
      <option value="dot_separation">Dot Separation (periods between characters)</option>
      <option value="character_replacement">Character Replacement (homoglyph substitution)</option>
    </select>
  </div>
  <button class="primary" onclick={saveBypassPrefill}>Save</button>
</div>

<div class="card">
  <h3>Context Budgeting</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    Allocates a percentage of the context window for injected content (cantrips, lorebooks, memory). Cantrips can access their allocation via <code>context.budget</code> to dynamically scale their output (full / summary / bullets). Set to 0 to disable.
  </p>
  <div class="form-group">
    <label for="budget-percent">Injection Budget (%)</label>
    <input id="budget-percent" type="number" bind:value={budgetPercent} min="0" max="100" step="0.5" style="width: 100px;">
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">Percentage of the context window reserved for cantrips and lorebooks.</p>
  </div>
  <div class="form-group">
    <label for="ctx-window">Context Window Override (tokens)</label>
    <input id="ctx-window" type="number" bind:value={contextWindowOverride} min="0" step="1000" style="width: 120px;">
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">Override the model's context window size. 0 = auto-detect from model name.</p>
  </div>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={debugMode} style="width: auto;">
      Debug Mode (capture pipeline stages for last 20 exchanges)
    </label>
  </div>
  <button class="primary" onclick={saveBudget}>Save</button>
</div>

<div class="card">
  <h3>API Key</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 8px;">
    Use this key as the Bearer token for proxy requests from JanitorAI or other clients.
  </p>
  {#if newKeyNotice}
    <div class="success-msg" style="margin-bottom: 8px;">
      New API key generated! Save this key now — it will not be shown again.
    </div>
  {/if}
  {#if !apiKey && !newKeyNotice}
    <p style="color: var(--text-dim); font-size: 11px; margin-bottom: 8px;">
      Your API key is not available (it was only shown once when your account was created).
      Click Regenerate to create a new one.
    </p>
  {/if}
  <div style="display: flex; gap: 8px; align-items: center;">
    <div class="api-key-display" style="flex: 1; margin: 0;">
      {#if showKey}
        {apiKey || 'Not available'}
      {:else}
        {apiKey ? '•'.repeat(Math.min(apiKey.length, 40)) : 'Not available'}
      {/if}
    </div>
    <button
      onclick={() => showKey = !showKey}
      title={showKey ? 'Hide' : 'Show'}
      disabled={!apiKey}
      style="flex-shrink: 0; padding: 8px 12px; font-size: 16px;"
    >{showKey ? '🙈' : '👁'}</button>
    <button
      onclick={copyKey}
      title="Copy"
      disabled={!apiKey}
      style="flex-shrink: 0; padding: 8px 12px; font-size: 16px;"
    >{copied ? '✓' : '⧉'}</button>
  </div>
  <button class="danger" onclick={regenerateKey} disabled={regenerating} style="margin-top: 8px; font-size: 12px;">
    {regenerating ? 'Regenerating...' : 'Regenerate API Key'}
  </button>
</div>
