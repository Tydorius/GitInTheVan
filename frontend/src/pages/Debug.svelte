<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let exchanges: any[] = []
  let loading = true
  let error = ''
  let selectedExchange: any = null
  let expandedStage: number | null = null

  async function load() {
    loading = true
    try {
      const data = await api.listDebugExchanges()
      exchanges = data.exchanges
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  async function viewExchange(id: string) {
    expandedStage = null
    try {
      selectedExchange = await api.getDebugExchange(id)
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

  function toggleStage(idx: number) {
    expandedStage = expandedStage === idx ? null : idx
  }

  function formatMessages(jsonStr: string | null): string {
    if (!jsonStr) return ''
    try {
      const msgs = typeof jsonStr === 'string' ? JSON.parse(jsonStr) : jsonStr
      return msgs.map((m: any) => `[${m.role}] ${typeof m.content === 'string' ? m.content : JSON.stringify(m.content)}`).join('\n\n')
    } catch {
      return String(jsonStr)
    }
  }

  function isResponseStage(stage: any): boolean {
    return stage.content_before !== undefined || stage.content_after !== undefined
  }

  function stageChanged(stage: any): boolean {
    if (isResponseStage(stage)) {
      return stage.content_before !== stage.content_after && stage.content_before !== undefined
    }
    return stage.messages_before !== stage.messages_after
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Debug <a class="help-link" href="/help/user-guide.html#debug" target="_blank" title="Open documentation">?</a></h2>
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
    <!-- Exchange list -->
    <div class="card" style="flex: 0 0 280px;">
      <h3 style="margin-bottom: 12px;">Recent Exchanges</h3>
      <div style="max-height: 70vh; overflow-y: auto;">
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

    <!-- Stage timeline -->
    {#if selectedExchange}
      <div class="card" style="flex: 1; min-width: 0;">
        <div class="card-header">
          <h3>Pipeline Timeline</h3>
          <span style="font-size: 11px; color: var(--text-dim);">
            {selectedExchange.pipeline_data.stages?.length || 0} stages
          </span>
        </div>

        <!-- Tags -->
        {#if selectedExchange.pipeline_data.tags?.length}
          <div style="margin-bottom: 12px;">
            <span style="font-size: 11px; color: var(--text-dim);">Tags: </span>
            {#each selectedExchange.pipeline_data.tags as tag}
              <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; margin-right: 4px;">{tag}</span>
            {/each}
          </div>
        {/if}

        <!-- Stage list -->
        {#if selectedExchange.pipeline_data.stages?.length}
          <div style="display: flex; flex-direction: column; gap: 2px;">
            {#each selectedExchange.pipeline_data.stages as stage, idx}
              <div style="border: 1px solid var(--border); border-radius: 4px; overflow: hidden;">
                <!-- Stage header (clickable) -->
                <div
                  onclick={() => toggleStage(idx)}
                  style="display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; background: var(--bg-elevated); font-size: 12px;"
                >
                  <span style="font-family: monospace; color: var(--text-dim); font-size: 10px; min-width: 28px;">{String(idx + 1).padStart(2, '0')}</span>
                  <span style="flex: 1; font-weight: 500;">{stage.label}</span>
                  {#if stage.detail}
                    <span style="color: var(--text-dim); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 300px;">{stage.detail}</span>
                  {/if}
                  {#if stageChanged(stage)}
                    <span style="font-size: 10px; padding: 1px 6px; border-radius: 3px; background: var(--accent); color: white;">changed</span>
                  {/if}
                  <span style="font-size: 12px; color: var(--text-dim);">{expandedStage === idx ? '▼' : '▶'}</span>
                </div>

                <!-- Stage detail (expandable) -->
                {#if expandedStage === idx}
                  <div style="padding: 12px; border-top: 1px solid var(--border); font-size: 12px;">
                    {#if stage.setting}
                      <div style="margin-bottom: 8px;">
                        <span style="color: var(--text-dim);">Setting: </span>
                        <code style="font-size: 11px;">{stage.setting}</code>
                        <span style="margin-left: 8px;">= </span>
                        <code style="font-size: 11px;">{JSON.stringify(stage.setting_value)}</code>
                      </div>
                    {/if}

                    {#if stage.detail}
                      <div style="margin-bottom: 8px; color: var(--text-dim);">{stage.detail}</div>
                    {/if}

                    <!-- Metadata -->
                    {#if stage.metadata && Object.keys(stage.metadata).length > 0}
                      <div style="margin-bottom: 8px;">
                        <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 4px;">Metadata:</div>
                        <pre style="background: var(--bg-elevated); padding: 8px; border-radius: 4px; font-size: 11px; overflow-x: auto; margin: 0;">{JSON.stringify(stage.metadata, null, 2)}</pre>
                      </div>
                    {/if}

                    <!-- Message diff (request stages) -->
                    {#if !isResponseStage(stage) && stage.messages_after}
                      {#if stage.messages_before && stage.messages_before !== stage.messages_after}
                        <div style="margin-bottom: 8px;">
                          <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 4px;">Before:</div>
                          <div style="background: var(--bg-elevated); padding: 8px; border-radius: 4px; white-space: pre-wrap; font-size: 11px; max-height: 200px; overflow-y: auto; font-family: monospace; border-left: 3px solid var(--border);">
                            {formatMessages(stage.messages_before)}
                          </div>
                        </div>
                      {/if}
                      <div>
                        <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 4px;">After:</div>
                        <div style="background: var(--bg-elevated); padding: 8px; border-radius: 4px; white-space: pre-wrap; font-size: 11px; max-height: 300px; overflow-y: auto; font-family: monospace; border-left: 3px solid var(--accent);">
                          {formatMessages(stage.messages_after)}
                        </div>
                      </div>
                    {/if}

                    <!-- Content diff (response stages) -->
                    {#if isResponseStage(stage)}
                      {#if stage.content_before && stage.content_before !== stage.content_after}
                        <div style="margin-bottom: 8px;">
                          <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 4px;">Before:</div>
                          <div style="background: var(--bg-elevated); padding: 8px; border-radius: 4px; white-space: pre-wrap; font-size: 11px; max-height: 200px; overflow-y: auto; font-family: monospace; border-left: 3px solid var(--border);">
                            {stage.content_before}
                          </div>
                        </div>
                      {/if}
                      {#if stage.content_after}
                        <div>
                          <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 4px;">After:</div>
                          <div style="background: var(--bg-elevated); padding: 8px; border-radius: 4px; white-space: pre-wrap; font-size: 11px; max-height: 300px; overflow-y: auto; font-family: monospace; border-left: 3px solid var(--accent);">
                            {stage.content_after}
                          </div>
                        </div>
                      {/if}
                    {/if}
                  </div>
                {/if}
              </div>
            {/each}
          </div>
        {:else}
          <div style="color: var(--text-dim); font-size: 12px; padding: 12px;">No stages captured (legacy exchange).</div>
        {/if}

        <!-- Full response -->
        {#if selectedExchange.response_content}
          <div style="margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px;">
            <h4 style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">Final Response Content</h4>
            <div style="background: var(--bg-elevated); padding: 12px; border-radius: 4px; white-space: pre-wrap; font-size: 12px; max-height: 400px; overflow-y: auto;">
              {selectedExchange.response_content}
            </div>
          </div>
        {/if}

        <!-- Verification details -->
        {#if selectedExchange.verification_data?.check_history?.length}
          <div style="margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px;">
            <h4 style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">Verification Details</h4>
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
      </div>
    {:else}
      <div class="card" style="flex: 1;">
        <div class="empty-state">Select an exchange to view pipeline details.</div>
      </div>
    {/if}
  </div>
{/if}
