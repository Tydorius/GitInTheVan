<script lang="ts">
  import { api, getApiKey } from '../api'
  import { onMount } from 'svelte'

  let settings = { default_endpoint_id: '' as string | null, default_model: '' }
  let endpoints: any[] = []
  let apiKey: string | null = null
  let error = ''
  let saved = false
  let showKey = false
  let copied = false

  async function load() {
    try {
      const [s, e] = await Promise.all([api.getSettings(), api.listEndpoints()])
      settings.default_endpoint_id = s.default_endpoint_id || ''
      settings.default_model = s.default_model || ''
      endpoints = e.endpoints
      apiKey = getApiKey()
    } catch (e: any) { error = e.message }
  }

  async function save() {
    error = ''; saved = false
    try { await api.updateSettings(settings); saved = true }
    catch (e: any) { error = e.message }
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
