<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let stats = { endpoints: 0, cantrips: 0, lorebooks: 0, rules: 0 }
  let health = ''

  onMount(async () => {
    try {
      const h = await api.health()
      health = h.status
    } catch { health = 'error' }

    try {
      const eps = await api.listEndpoints()
      stats.endpoints = eps.endpoints.length
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
  <h2>Dashboard</h2>
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
  <h3>Quick Start</h3>
  <div style="margin-top: 12px; color: var(--text-dim); line-height: 1.8;">
    <p>1. <a href="#/endpoints">Configure an endpoint</a> with your LLM API URL and key</p>
    <p>2. <a href="#/cantrips">Add cantrips</a> for dynamic lore injection (JanitorAI-compatible)</p>
    <p>3. <a href="#/lorebooks">Import lorebooks</a> for keyword-triggered world context</p>
    <p>4. <a href="#/verification">Set up verification</a> to auto-check LLM responses</p>
    <p>5. Point your client at <code style="color: var(--accent);">http://localhost:8000/v1/chat/completions</code> using your gitv_ API key</p>
  </div>
</div>
