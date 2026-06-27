<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let stats = { endpoints: 0, cantrips: 0, lorebooks: 0, rules: 0 }
  let health = ''
  let auditResults: any[] = []
  let auditRunning = false
  let auditDone = false
  let endpoints: any[] = []
  let selectedEndpoint = ''
  let endpointModels: string[] = []
  let selectedModel = ''
  let loadingModels = false
  let modelOverrideEnabled = false
  let modelOverrideText = ''
  let modelsAvailable = false

  async function loadModels() {
    endpointModels = []
    selectedModel = ''
    modelsAvailable = false
    modelOverrideEnabled = false
    if (!selectedEndpoint) return
    loadingModels = true
    try {
      const data = await fetch(`/api/endpoints/${selectedEndpoint}/models`, {
        headers: { 'Authorization': `Bearer ${localStorage.getItem('gitv_token')}` }
      }).then(r => r.json())
      endpointModels = data.models || []
      if (endpointModels.length > 0) {
        modelsAvailable = true
        const ep = endpoints.find(e => e.id === selectedEndpoint)
        if (ep?.default_model && endpointModels.includes(ep.default_model)) {
          selectedModel = ep.default_model
        } else {
          selectedModel = endpointModels[0]
        }
      } else {
        modelsAvailable = false
        modelOverrideEnabled = true
        const ep = endpoints.find(e => e.id === selectedEndpoint)
        if (ep?.default_model) {
          modelOverrideText = ep.default_model
        }
      }
    } catch {
      modelsAvailable = false
      modelOverrideEnabled = true
    }
    finally { loadingModels = false }
  }

  function toggleOverride() {
    modelOverrideEnabled = !modelOverrideEnabled
  }

  function effectiveModel(): string {
    if (modelOverrideEnabled) return modelOverrideText.trim()
    return selectedModel
  }

  async function runAudit() {
    auditRunning = true; auditDone = false
    try {
      const params = new URLSearchParams()
      if (selectedEndpoint) params.set('endpoint_id', selectedEndpoint)
      const model = effectiveModel()
      if (model) params.set('model', model)
      const qs = params.toString()
      const data = await fetch(`/api/diagnostics/audit${qs ? '?' + qs : ''}`, {
        headers: { 'Authorization': `Bearer ${localStorage.getItem('gitv_token')}` }
      }).then(r => r.json())
      auditResults = data.results
      auditDone = true
    } catch {}
    finally { auditRunning = false }
  }

  onMount(async () => {
    try {
      const h = await api.health()
      health = h.status
    } catch { health = 'error' }

    try {
      const eps = await api.listEndpoints()
      stats.endpoints = eps.endpoints.length
      endpoints = eps.endpoints
      if (endpoints.length > 0 && !selectedEndpoint) {
        selectedEndpoint = endpoints[0].id
        await loadModels()
      }
    } catch {}

    try {
      const cantrips = await api.listCantrips()
      stats.cantrips = cantrips.cantrips.length
    } catch {}

    try {
      const lbs = await api.listLorebooks()
      stats.lorebooks = lbs.lorebooks.length
    } catch {}

    try {
      const rules = await api.listVerificationRules()
      stats.rules = rules.rules.length
    } catch {}
  })
</script>

<div class="page-header">
  <h2>Dashboard <a class="help-link" href="/help/user-guide.html#dashboard" target="_blank" title="Open documentation">?</a></h2>
</div>

<div class="stats-grid">
  <div class="stat-card">
    <div class="stat-value">{stats.endpoints}</div>
    <div class="stat-label">Endpoints</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{stats.cantrips}</div>
    <div class="stat-label">Cantrips</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{stats.lorebooks}</div>
    <div class="stat-label">Lorebooks</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{stats.rules}</div>
    <div class="stat-label">Verification Rules</div>
  </div>
</div>

<div class="card">
  <h3>System Status</h3>
  <p style="margin-top: 12px;">Proxy health: <span class="badge {health === 'ok' ? 'active' : 'inactive'}">{health || 'checking...'}</span></p>
</div>

<div class="card">
  <h3>Diagnostics</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">Run a quick check of your endpoint, cantrip, and verification configuration.</p>
  {#if endpoints.length > 0}
    <div class="form-row">
      <div class="form-group" style="flex: 1;">
        <label for="diag-ep">Endpoint to Test</label>
        <select id="diag-ep" bind:value={selectedEndpoint} onchange={loadModels}>
          {#each endpoints as ep}<option value={ep.id}>{ep.name}</option>{/each}
        </select>
      </div>
      <div class="form-group" style="flex: 1;">
        <label for="diag-model">Model {#if loadingModels}(loading...){/if}</label>
        <select id="diag-model" bind:value={selectedModel} disabled={!modelsAvailable || modelOverrideEnabled}>
          {#if !modelsAvailable && !loadingModels}
            <option value="">No model list</option>
          {:else}
            <option value="">Use endpoint default</option>
            {#each endpointModels as m}<option value={m}>{m}</option>{/each}
          {/if}
        </select>
      </div>
    </div>
    <div class="form-group" style="display: flex; align-items: flex-start; gap: 8px; margin-bottom: 12px;">
      <label style="display: flex; align-items: center; gap: 4px; font-size: 12px; white-space: nowrap; padding-top: 6px;">
        <input type="checkbox" bind:checked={modelOverrideEnabled} onchange={toggleOverride} style="width: auto;">
        Override model
      </label>
      <input
        bind:value={modelOverrideText}
        placeholder="Type a model name (e.g. gemini-2.0-flash)"
        disabled={!modelOverrideEnabled}
        style="flex: 1;"
      />
    </div>
  {/if}
  <button class="primary" onclick={runAudit} disabled={auditRunning}>{auditRunning ? 'Checking...' : 'Run Diagnostic'}</button>

  {#if auditDone}
    <div style="margin-top: 16px;">
      {#each auditResults as r}
        <div style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border);">
          <span class="badge {r.passed ? 'approved' : 'violation'}">{r.passed ? 'OK' : 'FAIL'}</span>
          <strong style="font-size: 13px;">{r.check}:</strong>
          <span style="font-size: 12px; color: var(--text-dim);">{r.message}</span>
        </div>
        {#if r.detail}
          <div style="font-size: 11px; color: var(--warn); padding: 2px 0 8px 56px;">{r.detail}</div>
        {/if}
      {/each}
    </div>
  {/if}
</div>

<div class="card">
  <h3>Quick Start</h3>
  <div style="margin-top: 12px; color: var(--text-dim); line-height: 1.8;">
    <p>1. <a href="#/endpoints">Configure an endpoint</a> with your LLM API URL and key</p>
    <p>2. <a href="#/cantrips">Add cantrips</a> for dynamic lore injection (JanitorAI-compatible)</p>
    <p>3. <a href="#/lorebooks">Import lorebooks</a> for keyword-triggered world context</p>
    <p>4. <a href="#/verification">Set up verification</a> to auto-check LLM responses</p>
    <p>5. Point your client at <code style="color: var(--accent);">http://localhost:8000/v1/chat/completions</code> using your gitv_ API key</p>
  </div>
</div>
