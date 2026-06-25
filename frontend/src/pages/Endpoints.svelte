<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let endpoints: any[] = []
  let apiKeys: any[] = []
  let loading = true
  let error = ''
  let showForm = false
  let editingId: string | null = null

  let form = { name: '', base_url: '', api_key: '', api_base_path: '', provider: '', bypass_method: 'none', enabled: true }
  let showApiKey = false
  let visibleKeys: Record<string, boolean> = {}
  let copiedKeyId: string | null = null

  const providers = [
    { value: '', label: 'Custom (raw passthrough)' },
    { value: 'gemini', label: 'Google Gemini' },
    { value: 'openai', label: 'OpenAI' },
    { value: 'anthropic', label: 'Anthropic' },
    { value: 'openrouter', label: 'OpenRouter' },
    { value: 'openai_compatible', label: 'OpenAI-Compatible (OpenWebUI, vLLM, etc.)' },
    { value: 'deepseek', label: 'DeepSeek' },
    { value: 'ollama', label: 'Ollama' },
    { value: 'xai', label: 'xAI' },
  ]

  let showKeyForm = false
  let keyFormEndpointId: string | null = null
  let keyFormIsDefault = false
  let keyFormLabel = ''
  let newKeyResult = ''
  let newKeyId = ''

  function autoParseUrl() {
    const url = form.base_url.trim()
    if (!url) return
    const proto = url.indexOf('://')
    if (proto === -1) return
    const hostEnd = url.indexOf('/', proto + 3)
    if (hostEnd === -1) return
    const path = url.substring(hostEnd)
    form.base_url = url.substring(0, hostEnd)
    if (path.endsWith('/chat/completions')) {
      form.api_base_path = path.substring(0, path.length - '/chat/completions'.length)
    } else if (path.length > 1) {
      form.api_base_path = path
    }
  }

  async function load() {
    loading = true
    try {
      const [data, keys] = await Promise.all([api.listEndpoints(), api.listApiKeys()])
      endpoints = data.endpoints
      apiKeys = keys.keys
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  function resetForm() {
    form = { name: '', base_url: '', api_key: '', api_base_path: '', bypass_method: 'none', enabled: true }
    editingId = null
  }

  function startEdit(ep: any) {
    editingId = ep.id
    form = { name: ep.name, base_url: ep.base_url, api_key: ep.api_key, api_base_path: ep.api_base_path || '', provider: ep.provider || '', bypass_method: ep.bypass_method || 'none', enabled: ep.enabled }
    showForm = true
  }

  async function handleSubmit() {
    error = ''
    try {
      if (editingId) {
        await api.updateEndpoint(editingId, form)
      } else {
        await api.createEndpoint(form)
      }
      showForm = false
      resetForm()
      await load()
    } catch (e: any) { error = e.message }
  }

  async function handleDelete(id: string) {
    if (!confirm('Delete this endpoint?')) return
    try { await api.deleteEndpoint(id); await load() }
    catch (e: any) { error = e.message }
  }

  function getKeysForEndpoint(epId: string) {
    return apiKeys.filter((k: any) => k.endpoint_id === epId)
  }

  function openKeyForm(epId: string | null) {
    keyFormEndpointId = epId
    keyFormIsDefault = epId === null
    keyFormLabel = ''
    newKeyResult = ''
    newKeyId = ''
    showKeyForm = true
  }

  async function handleCreateKey() {
    error = ''
    try {
      const result = await api.createApiKey({ label: keyFormLabel || 'default', endpoint_id: keyFormEndpointId })
      newKeyResult = result.api_key
      newKeyId = result.id
      await load()
    } catch (e: any) { error = e.message }
  }

  async function deleteKey(keyId: string) {
    if (!confirm('Delete this API key? It will stop working immediately.')) return
    try { await api.deleteApiKey(keyId); await load() }
    catch (e: any) { error = e.message }
  }

  async function toggleKey(keyId: string) {
    try { await api.toggleApiKey(keyId); await load() }
    catch (e: any) { error = e.message }
  }

  function copyKey(text: string, id: string) {
    navigator.clipboard.writeText(text)
    copiedKeyId = id
    setTimeout(() => copiedKeyId = null, 2000)
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Endpoints <a class="help-link" href="/help/user-guide.html#endpoints" target="_blank" title="Open documentation">?</a></h2>
  <button class="primary" onclick={() => { resetForm(); showForm = true; }}>+ Add Endpoint</button>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

{#if loading}<div class="loading">Loading...</div>
{:else if endpoints.length === 0 && !showForm}
  <div class="empty-state">No endpoints configured. Click "Add Endpoint" to get started.</div>
{:else}
  {#each endpoints as ep}
    <div class="card">
      <div class="card-header">
        <div>
          <strong>{ep.name}</strong>
          <span class="badge {ep.enabled ? 'active' : 'inactive'}" style="margin-left: 8px;">
            {ep.enabled ? 'Enabled' : 'Disabled'}
          </span>
        </div>
        <div>
          <button onclick={() => startEdit(ep)}>Edit</button>
          <button class="danger" onclick={() => handleDelete(ep.id)}>Delete</button>
        </div>
      </div>
      <div style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">
        <div>URL: {ep.base_url}</div>
        <div>API Path: {ep.api_base_path || '/v1 (default)'}</div>
        <div>Provider Key: {ep.api_key ? ep.api_key.slice(0, 8) + '...' : 'none'}</div>
        <div>Provider: {ep.provider ? providers.find(p => p.value === ep.provider)?.label || ep.provider : 'Custom (passthrough)'}</div>
        <div>Bypass: {ep.bypass_method || 'none'}</div>
      </div>

      <div style="border-top: 1px solid var(--border); padding-top: 12px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <strong style="font-size: 13px;">GitInTheVan API Keys</strong>
          <button onclick={() => openKeyForm(ep.id)} style="font-size: 11px; padding: 2px 8px;">+ Add Key</button>
        </div>
        {#each getKeysForEndpoint(ep.id) as k}
          <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 6px; font-size: 12px;">
            <span class="badge {k.is_active ? 'active' : 'inactive'}" style="font-size: 10px;">{k.is_active ? 'ON' : 'OFF'}</span>
            <span style="color: var(--text-dim); min-width: 80px;">{k.label}</span>
            <span style="flex: 1; font-size: 10px; padding: 2px 6px; color: var(--text-dim);">
              gitv_•••••••• (shown only at creation)
            </span>
            <button onclick={() => toggleKey(k.id)} style="padding: 2px 8px; font-size: 11px;" title="Enable/Disable">{k.is_active ? 'Disable' : 'Enable'}</button>
            <button class="danger" onclick={() => deleteKey(k.id)} style="padding: 2px 8px; font-size: 11px;">Delete</button>
          </div>
        {/each}
        {#if getKeysForEndpoint(ep.id).length === 0}
          <p style="color: var(--text-dim); font-size: 11px;">No endpoint-specific keys. Requests using your default key will route here based on your Settings default.</p>
        {/if}
      </div>
    </div>
  {/each}

  <div class="card" style="margin-top: 16px;">
    <div style="display: flex; justify-content: space-between; align-items: center;">
      <div>
        <strong>Default API Keys</strong>
        <p style="color: var(--text-dim); font-size: 12px;">Routes to your default endpoint when no endpoint-specific key matches.</p>
      </div>
      <button onclick={() => openKeyForm(null)} style="font-size: 11px;">+ Add Default Key</button>
    </div>
    {#each apiKeys.filter((k: any) => !k.endpoint_id) as k}
      <div style="display: flex; gap: 8px; align-items: center; margin-top: 8px; font-size: 12px;">
        <span class="badge {k.is_active ? 'active' : 'inactive'}" style="font-size: 10px;">{k.is_active ? 'ON' : 'OFF'}</span>
        <span style="color: var(--text-dim); min-width: 80px;">{k.label}</span>
        <span style="flex: 1; font-size: 10px; padding: 2px 6px; color: var(--text-dim);">
          gitv_•••••••• (shown only at creation)
        </span>
        <button onclick={() => toggleKey(k.id)} style="padding: 2px 8px; font-size: 11px;">{k.is_active ? 'Disable' : 'Enable'}</button>
        <button class="danger" onclick={() => deleteKey(k.id)} style="padding: 2px 8px; font-size: 11px;">Delete</button>
      </div>
    {/each}
  </div>
{/if}

{#if showForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showForm = false; }}>
    <div class="modal">
      <h3>{editingId ? 'Edit Endpoint' : 'Add Endpoint'}</h3>
      {#if error}<div class="error-msg">{error}</div>{/if}
      <form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
        <div class="form-group">
          <label for="ep-name">Name</label>
          <input id="ep-name" autocomplete="off" bind:value={form.name} placeholder="OpenWebUI" required />
        </div>
        <div class="form-group">
          <label for="ep-url">Base URL <span style="color: var(--text-dim);">(paste full URL for auto-detect)</span></label>
          <input
            id="ep-url"
            autocomplete="off"
            spellcheck="false"
            bind:value={form.base_url}
            oninput={autoParseUrl}
            placeholder="https://owui.example.com or https://owui.example.com/api/chat/completions"
            required
          />
        </div>
        <div class="form-group">
          <label for="ep-key">Provider API Key</label>
          <div style="display: flex; gap: 8px; align-items: center;">
            <input
              id="ep-key"
              autocomplete="off"
              spellcheck="false"
              type={showApiKey ? "text" : "text"}
              style="flex: 1; -webkit-text-security: {showApiKey ? 'none' : 'disc'}; text-security: {showApiKey ? 'none' : 'disc'};"
              bind:value={form.api_key}
              placeholder="sk-..."
            />
            <button
              type="button"
              onclick={() => showApiKey = !showApiKey}
              style="flex-shrink: 0; padding: 8px 12px;"
            >{showApiKey ? '🙈' : '👁'}</button>
          </div>
        </div>
        <div class="form-group">
          <label for="ep-path">API Base Path <span style="color: var(--text-dim);">(leave blank for /v1)</span></label>
          <input id="ep-path" autocomplete="off" spellcheck="false" bind:value={form.api_base_path} placeholder="/api" />
        </div>
        <div class="form-group">
          <label for="ep-bypass">Content Bypass</label>
          <select id="ep-bypass" bind:value={form.bypass_method}>
            <option value="none">None (disabled)</option>
            <option value="space_separation">Space Separation (zero-width spaces)</option>
            <option value="dot_separation">Dot Separation (periods between characters)</option>
            <option value="character_replacement">Character Replacement (homoglyph substitution)</option>
          </select>
          <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            WARNING: May violate your provider's ToS. Use at your own risk.
          </p>
        </div>
        <div class="form-group">
          <label for="ep-provider">Provider <span style="color: var(--text-dim);">(enables LiteLLM compatibility)</span></label>
          <select id="ep-provider" bind:value={form.provider}>
            {#each providers as p}
              <option value={p.value}>{p.label}</option>
            {/each}
          </select>
          {#if form.provider}
            <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
              LiteLLM handles parameter compatibility, auth format, and response normalization automatically.
            </p>
          {:else}
            <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
              Raw HTTP passthrough. Use API base path for path rewriting if needed.
            </p>
          {/if}
        </div>
        <div class="form-group">
          <label for="ep-enabled">
            <input id="ep-enabled" type="checkbox" bind:checked={form.enabled} style="width: auto;"> Enabled
          </label>
        </div>
        <div class="modal-actions">
          <button onclick={() => { showForm = false; resetForm(); }}>Cancel</button>
          <button type="submit" class="primary">{editingId ? 'Update' : 'Create'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

{#if showKeyForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget && !newKeyResult) showKeyForm = false; }}>
    <div class="modal">
      {#if newKeyResult}
        <h3>API Key Created</h3>
        <div class="success-msg" style="margin-bottom: 12px;">
          Copy this key now — for security, only a hash is stored and the full key cannot be recovered.
        </div>
        <div class="form-group">
          <div class="api-key-display" style="cursor: pointer; user-select: all;" onclick={() => copyKey(newKeyResult, 'new')}>
            {copiedKeyId === 'new' ? 'Copied!' : newKeyResult}
          </div>
        </div>
        <div class="modal-actions">
          <button class="primary" onclick={() => { showKeyForm = false; newKeyResult = ''; }}>Done</button>
        </div>
      {:else}
        <h3>{keyFormIsDefault ? 'Add Default API Key' : 'Add Endpoint API Key'}</h3>
        {#if error}<div class="error-msg">{error}</div>{/if}
        <form onsubmit={(e) => { e.preventDefault(); handleCreateKey(); }}>
          <div class="form-group">
            <label for="key-label">Label</label>
            <input id="key-label" autocomplete="off" bind:value={keyFormLabel} placeholder={keyFormIsDefault ? 'Default' : 'JanitorAI'} />
          </div>
          <div class="modal-actions">
            <button onclick={() => showKeyForm = false}>Cancel</button>
            <button type="submit" class="primary">Create Key</button>
          </div>
        </form>
      {/if}
    </div>
  </div>
{/if}
