<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let exchanges: any[] = []
  let loading = true
  let error = ''
  let selectedExchange: any = null
  let viewMode: 'original' | 'modified' | 'response' = 'modified'

  async function load() {
    loading = true
    try {
      const data = await api.listDebugExchanges()
      exchanges = data.exchanges
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  async function viewExchange(id: string) {
    try {
      selectedExchange = await api.getDebugExchange(id)
      viewMode = 'modified'
    } catch (e: any) { error = e.message }
  }

  async function handleClear() {
    if (!confirm('Clear all debug exchanges?')) return
    try {
      await api.clearDebugExchanges()
      exchanges = []
      selectedExchange = null
    } catch (e: any) { error = e.message }
  }

  function formatMessages(jsonStr: string): string {
    try {
      const msgs = JSON.parse(jsonStr)
      return msgs.map((m: any) => `[${m.role}] ${typeof m.content === 'string' ? m.content : JSON.stringify(m.content)}`).join('\n\n')
    } catch {
      return jsonStr
    }
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Debug</h2>
  <div>
    <button onclick={load}>Refresh</button>
    <button class="danger" onclick={handleClear} disabled={exchanges.length === 0}>Clear All</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

{#if loading}<div class="loading">Loading...</div>
{:else if exchanges.length === 0 && !selectedExchange}
  <div class="empty-state">
    No debug exchanges captured. Enable Debug Mode in Settings to start capturing pipeline data.
  </div>
{:else}
  <div style="display: flex; gap: 16px; align-items: flex-start;">
    <div class="card" style="flex: 0 0 300px;">
      <h3 style="margin-bottom: 12px;">Recent Exchanges</h3>
      <div style="max-height: 600px; overflow-y: auto;">
        {#each exchanges as e}
          <div
            onclick={() => viewExchange(e.id)}
            style="padding: 8px 12px; cursor: pointer; border-bottom: 1px solid var(--border); font-size: 12px; {selectedExchange?.id === e.id ? 'background: var(--bg-elevated);' : ''}"
          >
            <div style="font-weight: 500;">{e.model || 'unknown'}</div>
            <div style="color: var(--text-dim); font-size: 11px;">
              {new Date(e.created_at).toLocaleString()}
            </div>
            <div style="color: var(--text-dim); font-size: 10px; margin-top: 2px;">
              {e.stage_count} stages{#if e.has_verification} | verified{/if}
            </div>
          </div>
        {/each}
      </div>
    </div>

    {#if selectedExchange}
      <div class="card" style="flex: 1;">
        <div class="card-header">
          <h3>Pipeline View</h3>
          <div style="display: flex; gap: 4px;">
            <button class={viewMode === 'original' ? 'primary' : ''} onclick={() => viewMode = 'original'} style="font-size: 12px;">Original</button>
            <button class={viewMode === 'modified' ? 'primary' : ''} onclick={() => viewMode = 'modified'} style="font-size: 12px;">Modified</button>
            <button class={viewMode === 'response' ? 'primary' : ''} onclick={() => viewMode = 'response'} style="font-size: 12px;">Response</button>
          </div>
        </div>

        {#if viewMode === 'original'}
          <div style="margin-bottom: 16px;">
            <h4 style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">Original Messages (before pipeline)</h4>
            <div style="background: var(--bg-elevated); padding: 12px; border-radius: 4px; white-space: pre-wrap; font-size: 12px; max-height: 500px; overflow-y: auto; font-family: monospace;">
              {formatMessages(selectedExchange.pipeline_data.original_messages || '[]')}
            </div>
          </div>
          {#if selectedExchange.pipeline_data.tags?.length}
            <div style="margin-bottom: 8px;">
              <span style="font-size: 11px; color: var(--text-dim);">Tags: </span>
              {#each selectedExchange.pipeline_data.tags as tag}
                <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; margin-right: 4px;">{tag}</span>
              {/each}
            </div>
          {/if}
        {:else if viewMode === 'modified'}
          <div style="margin-bottom: 16px;">
            <h4 style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">Modified Messages (sent to Driver)</h4>
            <div style="background: var(--bg-elevated); padding: 12px; border-radius: 4px; white-space: pre-wrap; font-size: 12px; max-height: 500px; overflow-y: auto; font-family: monospace;">
              {formatMessages(selectedExchange.pipeline_data.modified_messages || '[]')}
            </div>
          </div>
          {#if selectedExchange.pipeline_data.budget}
            <div style="margin-bottom: 16px;">
              <h4 style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">Budget</h4>
              <pre style="background: var(--bg-elevated); padding: 12px; border-radius: 4px; font-size: 11px; overflow-x: auto;">{JSON.stringify(selectedExchange.pipeline_data.budget, null, 2)}</pre>
            </div>
          {/if}
        {:else}
          <div style="margin-bottom: 16px;">
            <h4 style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">Driver Response</h4>
            <div style="background: var(--bg-elevated); padding: 12px; border-radius: 4px; white-space: pre-wrap; font-size: 12px; max-height: 400px; overflow-y: auto;">
              {selectedExchange.response_content || '(empty)'}
            </div>
          </div>
          {#if selectedExchange.verification_data?.check_history?.length}
            <div>
              <h4 style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">Verification</h4>
              <div style="background: var(--bg-elevated); padding: 12px; border-radius: 4px; font-size: 12px;">
                <div>Approved: <strong>{selectedExchange.verification_data.approved ? 'Yes' : 'No'}</strong></div>
                <div>Retries: {selectedExchange.verification_data.retries_used}</div>
                {#each selectedExchange.verification_data.check_history as check, i}
                  <div style="margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border);">
                    <div>Check {i + 1}: {check.approved ? 'PASS' : 'FAIL'}</div>
                    {#if check.violations?.length}
                      <ul style="margin: 4px 0; padding-left: 20px; font-size: 11px;">
                        {#each check.violations as v}<li>{v}</li>{/each}
                      </ul>
                    {/if}
                  </div>
                {/each}
              </div>
            </div>
          {/if}
        {/if}
      </div>
    {:else}
      <div class="card" style="flex: 1;">
        <div class="empty-state">Select an exchange to view pipeline details.</div>
      </div>
    {/if}
  </div>
{/if}
