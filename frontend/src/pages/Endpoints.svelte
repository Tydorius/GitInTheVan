<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let endpoints: any[] = []
  let loading = true
  let error = ''
  let showForm = false
  let editingId: string | null = null

  let form = { name: '', base_url: '', api_key: '', api_base_path: '', enabled: true }
  let showApiKey = false

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
      const data = await api.listEndpoints()
      endpoints = data.endpoints
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  function resetForm() {
    form = { name: '', base_url: '', api_key: '', api_base_path: '', enabled: true }
    editingId = null
  }

  function startEdit(ep: any) {
    editingId = ep.id
    form = { name: ep.name, base_url: ep.base_url, api_key: ep.api_key, api_base_path: ep.api_base_path || '', enabled: ep.enabled }
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

  onMount(load)
</script>

<div class="page-header">
  <h2>Endpoints</h2>
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
      <div style="color: var(--text-dim); font-size: 12px;">
        <div>URL: {ep.base_url}</div>
        <div>API Path: {ep.api_base_path || '/v1 (default)'}</div>
        <div>Key: {ep.api_key ? ep.api_key.slice(0, 8) + '...' : 'none'}</div>
      </div>
    </div>
  {/each}
{/if}

{#if showForm}
  <div class="modal-overlay" role="dialog" onclick={(e) => { if (e.target === e.currentTarget) showForm = false; }}>
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
          <label for="ep-key">API Key</label>
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
