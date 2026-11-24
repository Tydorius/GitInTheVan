<script lang="ts">
  /**
   * Single-run debug view.
   *
   * The stage timeline itself lives in RunColumn, shared with the comparison
   * view so the two cannot drift apart. This page adds the run list and the
   * per-run actions: save, rename, replay, export, and jumping to a comparison.
   */
  import { onMount, onDestroy } from 'svelte'
  import { api } from '../api'
  import type { DebugExchange, DebugExchangeListItem, DebugSandbox } from '../api'
  import { currentRoute } from '../stores'
  import { downloadFromApi } from '../lib/download'
  import { COMMON_TIMEZONES, formatTimestamp } from '../lib/timezone'
  import MetricsBar from '../lib/MetricsBar.svelte'
  import RunColumn from '../lib/RunColumn.svelte'

  let exchanges: DebugExchangeListItem[] = []
  let savedCount = 0
  let maxSaved = 10
  let loading = true
  let error = ''
  let notice = ''
  let selectedExchange: DebugExchange | null = null
  let sandboxes: DebugSandbox[] = []
  let editingSandboxId: string | null = null
  let editingPrompt = ''
  let userTimezone = ''
  let globalTimezone = ''
  let effectiveTimezone = ''
  let tzSaving = false
  let tzSaved = false
  let tzSavedTimer: ReturnType<typeof setTimeout> | null = null

  async function load() {
    loading = true
    try {
      const data = await api.listDebugExchanges()
      exchanges = data.exchanges
      savedCount = data.saved_count
      maxSaved = data.max_saved
      sandboxes = (await api.listDebugSandboxes()).sandboxes
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  async function createSandbox(id: string) {
    error = ''
    try {
      const name = prompt('Name this sandbox', selectedExchange?.label || 'Sandbox')
      if (name === null) return
      const sandbox = await api.createDebugSandbox(id, name)
      await load()
      notice = `Sandbox "${sandbox.name}" created. Run it as often as you like — ` +
               'the original conversation is untouched.'
    } catch (e: any) { error = e.message }
  }

  async function runSandbox(sandbox: DebugSandbox) {
    error = ''
    notice = ''
    try {
      const result = await api.runDebugSandbox(sandbox.id)
      await load()
      await viewExchange(result.run_id)
      notice = `Run ${result.run_count} complete.`
    } catch (e: any) { error = e.message }
  }

  async function resetSandbox(sandbox: DebugSandbox) {
    if (!confirm(
      `Reset "${sandbox.name}"?\n\n` +
      'Everything its runs accumulated is discarded and the conversation state ' +
      'is re-copied from the original. The original conversation is not affected.'
    )) return
    try {
      await api.resetDebugSandbox(sandbox.id)
      await load()
      notice = 'Sandbox reset.'
    } catch (e: any) { error = e.message }
  }

  async function deleteSandbox(sandbox: DebugSandbox) {
    if (!confirm(`Delete "${sandbox.name}" and everything its runs accumulated?`)) return
    try {
      await api.deleteDebugSandbox(sandbox.id)
      await load()
    } catch (e: any) { error = e.message }
  }

  function startEditPrompt(sandbox: DebugSandbox) {
    editingSandboxId = sandbox.id
    editingPrompt = sandbox.message_list.map((m) => `[${m.role}] ${m.content}`).join('\n\n')
  }

  async function savePrompt(sandbox: DebugSandbox) {
    // Round-trips the `[role] text` form the editor shows. Anything without a
    // recognised role prefix becomes a user message.
    const messages = editingPrompt
      .split(/\n\n+/)
      .map((block) => {
        const match = /^\[(system|user|assistant)\]\s*([\s\S]*)$/i.exec(block.trim())
        return match
          ? { role: match[1].toLowerCase(), content: match[2] }
          : { role: 'user', content: block.trim() }
      })
      .filter((m) => m.content)

    if (!messages.length) {
      error = 'A sandbox needs at least one message.'
      return
    }
    try {
      await api.updateDebugSandbox(sandbox.id, messages)
      editingSandboxId = null
      await load()
      notice = 'Prompt updated.'
    } catch (e: any) { error = e.message }
  }

  async function viewExchange(id: string) {
    try {
      selectedExchange = await api.getDebugExchange(id)
    } catch (e: any) { error = e.message }
  }

  async function handleClear() {
    if (!confirm('Clear all debug runs? Saved runs are kept.')) return
    try {
      await api.clearDebugExchanges()
      await load()
      if (selectedExchange && !exchanges.some((e) => e.id === selectedExchange!.id)) {
        selectedExchange = null
      }
    } catch (e: any) { error = e.message }
  }

  async function toggleSaved(item: DebugExchangeListItem, event: Event) {
    event.stopPropagation()
    error = ''
    try {
      if (item.saved) {
        await api.unsaveDebugExchange(item.id)
      } else {
        await api.saveDebugExchange(item.id, item.label || '')
      }
      await load()
      if (selectedExchange?.id === item.id) await viewExchange(item.id)
    } catch (e: any) { error = e.message }
  }

  async function rename(item: DebugExchangeListItem, event: Event) {
    event.stopPropagation()
    const label = prompt('Name this run', item.label || '')
    if (label === null) return
    try {
      await api.renameDebugExchange(item.id, label)
      await load()
    } catch (e: any) { error = e.message }
  }

  async function deleteRun(item: DebugExchangeListItem, event: Event) {
    event.stopPropagation()
    if (!confirm(`Delete run "${item.label || item.id.slice(0, 8)}"?`)) return
    try {
      await api.deleteDebugExchange(item.id)
      if (selectedExchange?.id === item.id) selectedExchange = null
      await load()
    } catch (e: any) { error = e.message }
  }

  async function replay(id: string) {
    if (!confirm(
      'Replay this run against your current configuration?\n\n' +
      'Cantrip side effects are NOT sandboxed: anything that writes user or ' +
      'cantrip data, rolls dice, or advances a counter will run again.'
    )) return

    error = ''
    notice = ''
    try {
      const result = await api.replayDebugExchange(id)
      await load()
      notice = 'Replay complete. Opening the comparison against the original.'
      currentRoute.set(`/debug/compare?ids=${id},${result.run_id}&baseline=${id}`)
      window.location.hash = `#/debug/compare?ids=${id},${result.run_id}&baseline=${id}`
    } catch (e: any) { error = e.message }
  }

  async function exportRun(id: string, format: 'json' | 'markdown') {
    try {
      await downloadFromApi(
        `/api/debug/${id}/export?format=${format}`,
        `gitv-debug.${format === 'json' ? 'json' : 'md'}`,
      )
    } catch (e: any) { error = e.message }
  }

  function openCompare() {
    const ids = exchanges.slice(0, 2).map((e) => e.id)
    const query = ids.length >= 2 ? `?ids=${ids.join(',')}&baseline=${ids[0]}` : ''
    window.location.hash = `#/debug/compare${query}`
  }

  async function loadTimezone() {
    try {
      const s = await api.getSettings()
      userTimezone = s.timezone || ''
      globalTimezone = s.global_timezone || ''
      effectiveTimezone = s.effective_timezone ?? (userTimezone || globalTimezone || '')
    } catch {
      // default to browser local
    }
  }

  async function handleTimezoneChange(event: Event) {
    const value = (event.currentTarget as HTMLSelectElement).value
    userTimezone = value
    error = ''
    tzSaving = true
    tzSaved = false
    try {
      const res = await api.updateSettings({ timezone: value })
      userTimezone = res?.timezone ?? value
      globalTimezone = res?.global_timezone ?? globalTimezone
      effectiveTimezone = res?.effective_timezone ?? (userTimezone || globalTimezone || '')
      tzSaved = true
      if (tzSavedTimer) clearTimeout(tzSavedTimer)
      tzSavedTimer = setTimeout(() => { tzSaved = false }, 2000)
    } catch (e: any) {
      error = e.message
    } finally {
      tzSaving = false
    }
  }

  onMount(() => {
    load()
    loadTimezone()
  })

  onDestroy(() => {
    if (tzSavedTimer) clearTimeout(tzSavedTimer)
  })
</script>

<div class="toolbar">
  <div class="saved-count">
    {savedCount} of {maxSaved} save slot{maxSaved === 1 ? '' : 's'} used
  </div>
  <div class="toolbar-actions">
    <label class="tz-control">
      <span>Timezone</span>
      <select value={userTimezone} onchange={handleTimezoneChange} disabled={tzSaving}>
        <option value="">Global default ({globalTimezone || 'Browser Local'})</option>
        {#if userTimezone && !COMMON_TIMEZONES.some((tz) => tz.value === userTimezone)}
          <option value={userTimezone}>{userTimezone}</option>
        {/if}
        {#each COMMON_TIMEZONES as tz}
          <option value={tz.value}>{tz.label}</option>
        {/each}
      </select>
      {#if tzSaved}<span class="tz-saved">Saved</span>{/if}
    </label>
    <button onclick={openCompare} disabled={exchanges.length < 2}>Compare runs</button>
    <button onclick={load}>Refresh</button>
    <button class="danger" onclick={handleClear} disabled={exchanges.length === 0}>Clear All</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if notice}<div class="success-msg">{notice}</div>{/if}

{#if loading}
  <div class="loading">Loading...</div>
{:else if exchanges.length === 0 && !selectedExchange}
  <div class="empty-state">
    No debug runs captured. Enable Debug Mode in Settings, then send a message through the
    proxy. Streaming and non-streaming requests are both captured.
  </div>
{:else}
  {#if sandboxes.length}
    <div class="card sandbox-card">
      <div class="card-header">
        <h3>Sandboxes</h3>
        <span class="hint">
          Forked copies you can re-run from here. Each keeps its own conversation
          state, so running one never affects the chat it came from — and nothing
          is ever sent back to the service the request originally came from.
        </span>
      </div>

      {#each sandboxes as sb (sb.id)}
        <div class="sandbox">
          <div class="sandbox-head">
            <strong>{sb.name}</strong>
            <span class="run-meta">
              {sb.run_count} run{sb.run_count === 1 ? '' : 's'}
              · {sb.message_count} message{sb.message_count === 1 ? '' : 's'}
              {#if sb.last_run_at}· last {formatTimestamp(sb.last_run_at, effectiveTimezone)}{/if}
            </span>
            <div class="sandbox-actions">
              <button class="primary" onclick={() => runSandbox(sb)}>Run</button>
              <button onclick={() => startEditPrompt(sb)}>Edit prompt</button>
              <button onclick={() => resetSandbox(sb)} title="Discard accumulated state and re-copy from the original">
                Reset
              </button>
              <button class="danger" onclick={() => deleteSandbox(sb)}>Delete</button>
            </div>
          </div>

          {#if editingSandboxId === sb.id}
            <div class="prompt-editor">
              <label for={`sb-prompt-${sb.id}`}>
                One message per block, separated by a blank line. Prefix with
                <code>[system]</code>, <code>[user]</code> or <code>[assistant]</code>.
              </label>
              <textarea id={`sb-prompt-${sb.id}`} bind:value={editingPrompt} rows="8"></textarea>
              <div class="sandbox-actions">
                <button class="primary" onclick={() => savePrompt(sb)}>Save</button>
                <button onclick={() => editingSandboxId = null}>Cancel</button>
              </div>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  <div class="layout">
    <div class="card list-card">
      <h3>Recent Runs</h3>
      <div class="list">
        {#each exchanges as e (e.id)}
          <div
            class="run"
            class:active={selectedExchange?.id === e.id}
            class:saved={e.saved}
            role="button"
            tabindex="0"
            onclick={() => viewExchange(e.id)}
            onkeydown={(ev) => ev.key === 'Enter' && viewExchange(e.id)}
          >
            <div class="run-name">
              {e.label || e.model || 'unknown'}
              {#if e.saved}<span class="badge">saved</span>{/if}
              {#if e.source === 'replay'}<span class="badge">replay</span>{/if}
            </div>
            <div class="run-meta">{formatTimestamp(e.created_at, effectiveTimezone)}</div>
            <div class="run-meta">
              {e.stage_count} stages{#if e.has_verification} · verified{/if}
              {#if e.totals?.total_tokens}
                · {e.totals.total_tokens.toLocaleString()} tok{e.totals.tokens_source === 'estimated' ? ' est' : ''}
              {/if}
              {#if (e.totals?.cached_tokens ?? 0) > 0}
                · {(e.totals.cached_tokens ?? 0).toLocaleString()} cached{#if (e.totals?.prompt_tokens ?? 0) > 0} ({Math.round(((e.totals.cached_tokens ?? 0) / e.totals.prompt_tokens) * 100)}%){/if}
              {/if}
              {#if e.totals?.total_latency_ms}
                · {(e.totals.total_latency_ms / 1000).toFixed(1)}s
              {/if}
            </div>
            <div class="run-actions">
              <button onclick={(ev) => toggleSaved(e, ev)} title={e.saved ? 'Unpin this run' : 'Pin so retention cannot evict it'}>
                {e.saved ? 'Unsave' : 'Save'}
              </button>
              <button onclick={(ev) => rename(e, ev)}>Rename</button>
              <button onclick={(ev) => deleteRun(e, ev)}>Delete</button>
            </div>
          </div>
        {/each}
      </div>
    </div>

    {#if selectedExchange}
      <div class="card detail-card">
        <div class="card-header">
          <h3>{selectedExchange.label || 'Run detail'}</h3>
          <div class="detail-actions">
            <button onclick={() => createSandbox(selectedExchange!.id)}
                    title="Fork this request so you can re-run it from here, as often as you like, without touching the original chat">
              Create sandbox copy
            </button>
            <button onclick={() => replay(selectedExchange!.id)}
                    title="Run this exact request once against your current config and compare">
              Replay
            </button>
            <button onclick={() => exportRun(selectedExchange!.id, 'markdown')}>Export .md</button>
            <button onclick={() => exportRun(selectedExchange!.id, 'json')}>Export .json</button>
          </div>
        </div>

        <MetricsBar totals={selectedExchange.pipeline_data?.run?.totals} />
        {#if selectedExchange.cache_analysis?.available}
          {@const ca = selectedExchange.cache_analysis}
          {@const pct = ca.prefix_stability_pct ?? 0}
          {@const isGood = Boolean(ca.is_tail_only) || pct >= 80}
          {@const isBad = !isGood && pct < 50}
          <div
            class="cache-banner"
            class:cache-good={isGood}
            class:cache-warn={!isGood && !isBad}
            class:cache-bad={isBad}
          >
            <div class="cache-banner-head">
              <span class="cache-banner-title">Prompt Cache Analysis</span>
              <span class="cache-banner-stat">
                <strong>Prefix Stability: {pct}%</strong>
                <span class="cache-banner-tokens">
                  ({(ca.shared_prefix_tokens ?? 0).toLocaleString()} / {(ca.total_prompt_tokens ?? 0).toLocaleString()} tokens shared with previous turn)
                </span>
              </span>
              {#if ca.culprit_stage}
                <span class="cache-culprit" title="First pipeline stage that diverged from the previous turn's prefix">
                  Culprit: {ca.culprit_stage}
                </span>
              {/if}
            </div>
            {#if ca.summary}
              <div class="cache-banner-summary">{ca.summary}</div>
            {/if}
          </div>
        {/if}
        <RunColumn exchange={selectedExchange} />
      </div>
    {:else}
      <div class="card detail-card">
        <div class="empty-state">Select a run to inspect it.</div>
      </div>
    {/if}
  </div>
{/if}

<style>
  .toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
  }
  .saved-count { font-size: 12px; color: var(--text-dim); }
  .toolbar-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .tz-control {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-dim);
  }
  .tz-control select { width: auto; font-size: 12px; padding: 4px 8px; }
  .tz-saved { font-size: 11px; color: var(--success); }

  .layout { display: flex; gap: 16px; align-items: flex-start; }
  .list-card { flex: 0 0 300px; }
  .detail-card { flex: 1; min-width: 0; }
  .list { max-height: 70vh; overflow-y: auto; }

  .run {
    padding: 8px 10px;
    cursor: pointer;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
  }
  .run.active { background: var(--surface2); }
  .run.saved { border-left: 3px solid var(--accent); }
  .run-name { font-weight: 500; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .run-meta { color: var(--text-dim); font-size: 11px; margin-top: 2px; }
  .run-actions { display: flex; gap: 4px; margin-top: 6px; }
  .run-actions button { font-size: 10px; padding: 2px 6px; }

  .detail-actions { display: flex; gap: 6px; flex-wrap: wrap; }

  .cache-banner {
    margin: 12px 0;
    padding: 10px 14px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    border-left-width: 4px;
    background: var(--surface2);
    font-size: 12px;
  }
  .cache-banner.cache-good {
    border-left-color: var(--success);
    background: rgba(61, 214, 140, 0.08);
  }
  .cache-banner.cache-warn {
    border-left-color: var(--warn);
    background: rgba(255, 180, 67, 0.08);
  }
  .cache-banner.cache-bad {
    border-left-color: var(--danger);
    background: rgba(229, 72, 77, 0.09);
  }
  .cache-banner-head {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .cache-banner-title {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-dim);
  }
  .cache-banner-stat {
    color: var(--text);
  }
  .cache-banner-tokens {
    color: var(--text-dim);
    margin-left: 4px;
  }
  .cache-culprit {
    font-size: 11px;
    font-family: var(--mono);
    padding: 2px 7px;
    border-radius: 999px;
    border: 1px solid var(--warn);
    color: var(--warn);
    background: rgba(255, 180, 67, 0.12);
    margin-left: auto;
  }
  .cache-banner.cache-bad .cache-culprit {
    border-color: var(--danger);
    color: var(--danger);
    background: rgba(229, 72, 77, 0.14);
  }
  .cache-banner-summary {
    margin-top: 6px;
    color: var(--text-dim);
    line-height: 1.45;
  }

  .sandbox-card { margin-bottom: 16px; }
  .hint {
    font-size: 12px;
    color: var(--text-dim);
    font-weight: normal;
    max-width: 60ch;
  }
  .sandbox {
    border-top: 1px solid var(--border);
    padding: 10px 0;
  }
  .sandbox-head {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .sandbox-actions { display: flex; gap: 4px; margin-left: auto; }
  .sandbox-actions button { font-size: 11px; padding: 3px 8px; }
  .prompt-editor { margin-top: 8px; }
  .prompt-editor label {
    display: block;
    font-size: 11px;
    color: var(--text-dim);
    margin-bottom: 4px;
  }
  .prompt-editor textarea {
    width: 100%;
    font-family: var(--mono);
    font-size: 12px;
  }
  .prompt-editor .sandbox-actions { margin-left: 0; margin-top: 6px; }
</style>
