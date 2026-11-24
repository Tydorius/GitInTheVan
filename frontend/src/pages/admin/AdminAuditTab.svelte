<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { api } from '../../api'
  import { formatTimestamp } from '../../lib/timezone'

  let auditLogs: any[] = $state([])
  let error = $state('')
  let auditAutoRefresh = $state(false)
  let auditInterval: ReturnType<typeof setInterval> | null = null
  let logTimezone = $state('')

  async function loadAudit() {
    try {
      const data = await api.getAdminAuditLogs(100, 0)
      auditLogs = data.logs
    } catch (e: any) { error = e.message }
  }

  async function loadTimezone() {
    try {
      const settings = await api.getAdminSettings()
      logTimezone = settings.log_timezone || ''
    } catch {
      // default to browser local
    }
  }

  function toggleAuditAutoRefresh() {
    auditAutoRefresh = !auditAutoRefresh
    if (auditAutoRefresh) {
      auditInterval = setInterval(loadAudit, 15000)
    } else if (auditInterval) {
      clearInterval(auditInterval)
      auditInterval = null
    }
  }

  onMount(() => {
    loadAudit()
    loadTimezone()
  })

  onDestroy(() => {
    if (auditInterval) clearInterval(auditInterval)
  })
</script>

{#if error}<div class="error-msg">{error}</div>{/if}

<div style="margin-bottom: 12px;">
  <button onclick={loadAudit}>Refresh</button>
  <button onclick={toggleAuditAutoRefresh} class={auditAutoRefresh ? 'primary' : ''} style="margin-left: 8px;">
    {auditAutoRefresh ? 'Auto ON (15s)' : 'Auto OFF'}
  </button>
</div>

{#if auditLogs.length === 0}
  <div class="empty-state">No audit logs recorded.</div>
{:else}
  <table>
    <thead><tr><th>Action</th><th>User</th><th>Target</th><th>Details</th><th>Time</th></tr></thead>
    <tbody>
      {#each auditLogs as log}
        <tr>
          <td><strong>{log.action}</strong></td>
          <td style="font-size: 11px;">{log.username || (log.user_id || '').slice(0, 8)}</td>
          <td style="font-size: 11px;">{log.target_type}{#if log.target_id} ({log.target_id.slice(0, 8)}){/if}</td>
          <td style="font-size: 12px; max-width: 300px; overflow: hidden; text-overflow: ellipsis;">{log.details}</td>
          <td style="color: var(--text-dim); font-size: 11px;">{formatTimestamp(log.created_at, logTimezone)}</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}
