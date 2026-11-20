<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { api } from '../../api'

  let serverLogs: string[] = $state([])
  let error = $state('')
  let serverLogAutoRefresh = $state(false)
  let serverLogInterval: ReturnType<typeof setInterval> | null = null

  async function loadServerLogs() {
    try {
      const data = await api.getServerLogs(200)
      serverLogs = data.lines
    } catch (e: any) { error = e.message }
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

  onMount(loadServerLogs)

  onDestroy(() => {
    if (serverLogInterval) clearInterval(serverLogInterval)
  })
</script>

{#if error}<div class="error-msg">{error}</div>{/if}

<div style="margin-bottom: 12px;">
  <button onclick={loadServerLogs}>Refresh</button>
  <button onclick={toggleServerLogAutoRefresh} class={serverLogAutoRefresh ? 'primary' : ''} style="margin-left: 8px;">
    {serverLogAutoRefresh ? 'Auto ON (15s)' : 'Auto OFF'}
  </button>
</div>
<div class="card" style="background: #1a1d27; padding: 0;">
  <div id="server-log-view" style="
    max-height: 600px; overflow-y: auto; padding: 12px;
    font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 11px; line-height: 1.6; color: #c8c9d3;
    white-space: pre-wrap; word-break: break-all;
  ">{serverLogs.join('\n')}</div>
</div>
