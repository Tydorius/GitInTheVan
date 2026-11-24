<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { api } from '../../api'
  import { COMMON_TIMEZONES } from '../../lib/timezone'

  let serverLogs: string[] = $state([])
  let error = $state('')
  let serverLogAutoRefresh = $state(false)
  let serverLogInterval: ReturnType<typeof setInterval> | null = null
  let logTimezone = $state('')
  let tzSaving = $state(false)
  let tzSaved = $state(false)
  let tzSavedTimer: ReturnType<typeof setTimeout> | null = null

  async function loadServerLogs() {
    try {
      const data = await api.getServerLogs(200)
      serverLogs = data.lines
    } catch (e: any) { error = e.message }
  }

  async function loadTimezone() {
    try {
      const settings = await api.getAdminSettings()
      logTimezone = settings.log_timezone || ''
    } catch {
      // keep default
    }
  }

  async function handleTimezoneChange(e: Event) {
    const value = (e.currentTarget as HTMLSelectElement).value
    logTimezone = value
    error = ''
    tzSaving = true
    tzSaved = false
    try {
      const updated = await api.updateAdminSettings({ log_timezone: value })
      logTimezone = updated.log_timezone || ''
      tzSaved = true
      if (tzSavedTimer) clearTimeout(tzSavedTimer)
      tzSavedTimer = setTimeout(() => { tzSaved = false }, 2000)
      await loadServerLogs()
    } catch (err: any) {
      error = err.message
    } finally {
      tzSaving = false
    }
  }

  function toggleServerLogAutoRefresh() {
    serverLogAutoRefresh = !serverLogAutoRefresh
    if (serverLogAutoRefresh) {
      serverLogInterval = setInterval(loadServerLogs, 15000)
    } else if (serverLogInterval) {
      clearInterval(serverLogInterval)
      serverLogInterval = null
    }
  }

  onMount(() => {
    loadServerLogs()
    loadTimezone()
  })

  onDestroy(() => {
    if (serverLogInterval) clearInterval(serverLogInterval)
    if (tzSavedTimer) clearTimeout(tzSavedTimer)
  })
</script>

{#if error}<div class="error-msg">{error}</div>{/if}

<div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;">
  <button onclick={loadServerLogs}>Refresh</button>
  <button onclick={toggleServerLogAutoRefresh} class={serverLogAutoRefresh ? 'primary' : ''}>
    {serverLogAutoRefresh ? 'Auto ON (15s)' : 'Auto OFF'}
  </button>
  <label for="admin-log-timezone" style="margin-left: auto; font-size: 12px; color: var(--text-dim);">
    Log Timezone
  </label>
  <select
    id="admin-log-timezone"
    value={logTimezone}
    onchange={handleTimezoneChange}
    disabled={tzSaving}
    style="width: auto;"
  >
    <option value="">Server / Browser Local</option>
    {#if logTimezone && !COMMON_TIMEZONES.some((tz) => tz.value === logTimezone)}
      <option value={logTimezone}>{logTimezone}</option>
    {/if}
    {#each COMMON_TIMEZONES as tz}
      <option value={tz.value}>{tz.label}</option>
    {/each}
  </select>
  {#if tzSaved}<span style="font-size: 12px; color: var(--success);">Saved</span>{/if}
</div>
<div class="card" style="background: #1a1d27; padding: 0;">
  <div id="server-log-view" style="
    max-height: 600px; overflow-y: auto; padding: 12px;
    font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 11px; line-height: 1.6; color: #c8c9d3;
    white-space: pre-wrap; word-break: break-all;
  ">{serverLogs.join('\n')}</div>
</div>
