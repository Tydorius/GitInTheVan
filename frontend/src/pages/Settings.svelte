<script lang="ts">
  import { api, getApiKey } from '../api'
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
  let endpoints: any[] = []
  let apiKey: string | null = null
  let error = ''
  let saved = false
  let sumSaved = false
  let showKey = false
  let copied = false

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

  function copyKey() {
    if (!apiKey) return
    navigator.clipboard.writeText(apiKey)
    copied = true
    setTimeout(() => copied = false, 2000)
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
  <h3>API Key</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 8px;">
    Use this key as the Bearer token for proxy requests from JanitorAI or other clients.
  </p>
  <div style="display: flex; gap: 8px; align-items: center;">
    <div class="api-key-display" style="flex: 1; margin: 0;">
      {#if showKey}
        {apiKey || 'Not available'}
      {:else}
        {'•'.repeat(Math.min((apiKey || '').length, 40))}
      {/if}
    </div>
    <button
      onclick={() => showKey = !showKey}
      title={showKey ? 'Hide' : 'Show'}
      style="flex-shrink: 0; padding: 8px 12px; font-size: 16px;"
    >{showKey ? '🙈' : '👁'}</button>
    <button
      onclick={copyKey}
      title="Copy"
      disabled={!apiKey}
      style="flex-shrink: 0; padding: 8px 12px; font-size: 16px;"
    >{copied ? '✓' : '⧉'}</button>
  </div>
</div>
