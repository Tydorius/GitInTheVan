<script lang="ts">
  /**
   * Side-by-side comparison of two to four debug runs.
   *
   * The baseline radio at the top of each column is the mechanism that makes
   * iterative A/B testing cheap: choosing a new baseline moves that run to the
   * first column and recomputes every diff against it, without re-running
   * anything. The selection and baseline live in the URL so the comparison
   * survives a reload and can be bookmarked — which matters because the object
   * links inside it open in new tabs.
   */
  import { onMount } from 'svelte'
  import { api } from '../api'
  import type { DebugExchange, DebugExchangeListItem } from '../api'
  import { routeParams } from '../stores'
  import { replaceRouteParams } from '../lib/deeplink'
  import { downloadFromApi } from '../lib/download'
  import GitvLink from '../lib/GitvLink.svelte'
  import MetricsBar from '../lib/MetricsBar.svelte'
  import RunColumn from '../lib/RunColumn.svelte'

  const MAX_RUNS = 4

  let available: DebugExchangeListItem[] = []
  let selectedIds: string[] = []
  let baselineId = ''
  let exchanges: Record<string, DebugExchange> = {}
  let comparison: any = null
  let loading = false
  let error = ''
  let notice = ''
  let showTimelines = false

  $: params = $routeParams.params
  $: orderedIds = comparison?.order || selectedIds

  onMount(async () => {
    await loadAvailable()

    const fromUrl = (params.ids || '').split(',').filter(Boolean)
    if (fromUrl.length >= 2) {
      selectedIds = fromUrl.slice(0, MAX_RUNS)
      baselineId = params.baseline && selectedIds.includes(params.baseline)
        ? params.baseline
        : selectedIds[0]
      await runComparison()
    }
  })

  async function loadAvailable() {
    try {
      const data = await api.listDebugExchanges()
      available = data.exchanges
    } catch (e: any) {
      error = e.message
    }
  }

  function toggleRun(id: string) {
    if (selectedIds.includes(id)) {
      selectedIds = selectedIds.filter((s) => s !== id)
      if (baselineId === id) baselineId = selectedIds[0] || ''
    } else {
      if (selectedIds.length >= MAX_RUNS) {
        notice = `You can compare at most ${MAX_RUNS} runs at once.`
        return
      }
      selectedIds = [...selectedIds, id]
      if (!baselineId) baselineId = id
    }
    notice = ''
    comparison = null
  }

  async function runComparison() {
    if (selectedIds.length < 2) {
      error = 'Select at least two runs to compare.'
      return
    }
    loading = true
    error = ''
    try {
      // Fetch the full exchanges alongside the diff: the diff names what
      // changed, the exchanges carry what to render.
      const loaded = await Promise.all(selectedIds.map((id) => api.getDebugExchange(id)))
      exchanges = Object.fromEntries(loaded.map((e) => [e.id, e]))
      comparison = await api.compareDebugExchanges(selectedIds, baselineId || selectedIds[0])
      baselineId = comparison.baseline_id
      syncUrl()
    } catch (e: any) {
      error = e.message
      comparison = null
    } finally {
      loading = false
    }
  }

  /** Re-baseline without re-running anything. */
  async function setBaseline(id: string) {
    if (id === baselineId) return
    baselineId = id
    if (comparison) {
      loading = true
      try {
        comparison = await api.compareDebugExchanges(selectedIds, id)
        syncUrl()
      } catch (e: any) {
        error = e.message
      } finally {
        loading = false
      }
    }
  }

  function syncUrl() {
    replaceRouteParams('/debug/compare', {
      ids: selectedIds.join(','),
      baseline: baselineId,
    })
  }

  async function exportComparison(format: 'json' | 'markdown') {
    try {
      await downloadFromApi(
        `/api/debug/compare/export?format=${format}`,
        `gitv-comparison.${format === 'json' ? 'json' : 'md'}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids: selectedIds, baseline_id: baselineId }),
        },
      )
    } catch (e: any) {
      error = e.message
    }
  }

  async function replay(id: string) {
    if (!confirm(
      'Replay this run against your current configuration?\n\n' +
      'Cantrip side effects are NOT sandboxed: anything that writes user or ' +
      'cantrip data, rolls dice, or advances a counter will run again.'
    )) return

    loading = true
    try {
      const result = await api.replayDebugExchange(id)
      await loadAvailable()
      // Drop in beside its source so the new run is immediately comparable.
      if (!selectedIds.includes(result.run_id)) {
        selectedIds = selectedIds.length >= MAX_RUNS
          ? [...selectedIds.slice(0, MAX_RUNS - 1), result.run_id]
          : [...selectedIds, result.run_id]
      }
      baselineId = id
      await runComparison()
      notice = 'Replay complete. The new run is the rightmost column.'
    } catch (e: any) {
      error = e.message
    } finally {
      loading = false
    }
  }

  function runName(id: string): string {
    const item = available.find((a) => a.id === id)
    return item?.label || id.slice(0, 8)
  }

  function diffFor(id: string): any {
    return comparison?.diffs?.[id] || null
  }

  function totalsFor(id: string) {
    return exchanges[id]?.pipeline_data?.run?.totals || {}
  }

  /** Count of everything that differs, for the column's summary line. */
  function changeCount(d: any): number {
    if (!d) return 0
    const c = d.cantrips || {}
    return (
      (c.added?.length || 0) + (c.removed?.length || 0) +
      (c.code_changed?.length || 0) + (c.output_changed?.length || 0) +
      (c.trigger_changed?.length || 0) + (c.data_changed?.length || 0) +
      (d.lorebooks?.added?.length || 0) + (d.lorebooks?.removed?.length || 0) +
      (d.skills?.added?.length || 0) + (d.skills?.removed?.length || 0) +
      (d.stages?.only_in_baseline?.length || 0) + (d.stages?.only_in_other?.length || 0) +
      (d.stages?.changed?.length || 0) +
      (d.identity?.same ? 0 : 1) +
      (d.verification?.same ? 0 : 1) +
      (d.response?.identical ? 0 : 1)
    )
  }

  function nearEviction(item: DebugExchangeListItem): boolean {
    if (item.saved) return false
    const unsaved = available.filter((a) => !a.saved)
    return unsaved.indexOf(item) >= unsaved.length - 3
  }
</script>

<div class="page-header">
  <h1>Compare Debug Runs</h1>
  <div class="actions">
    <button onclick={loadAvailable}>Refresh</button>
    <button class="primary" onclick={runComparison} disabled={selectedIds.length < 2 || loading}>
      Compare {selectedIds.length} run{selectedIds.length === 1 ? '' : 's'}
    </button>
    {#if comparison}
      <button onclick={() => exportComparison('markdown')}>Export .md</button>
      <button onclick={() => exportComparison('json')}>Export .json</button>
    {/if}
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if notice}<div class="success-msg">{notice}</div>{/if}

<div class="card">
  <div class="card-header">Select runs (up to {MAX_RUNS})</div>
  {#if !available.length}
    <div class="empty-state">
      No debug runs recorded. Enable debug mode in Settings, then send a message through
      the proxy.
    </div>
  {:else}
    <div class="picker">
      {#each available as item (item.id)}
        <button
          class="pick"
          class:active={selectedIds.includes(item.id)}
          onclick={() => toggleRun(item.id)}
        >
          <span class="pick-name">
            {item.label || item.id.slice(0, 8)}
            {#if item.saved}<span class="badge">saved</span>{/if}
            {#if item.source === 'replay'}<span class="badge">replay</span>{/if}
          </span>
          <span class="pick-meta">
            {new Date(item.created_at).toLocaleString()}
            · {item.stage_count} stages
            {#if item.totals?.total_tokens}· {item.totals.total_tokens.toLocaleString()} tok{/if}
          </span>
          {#if nearEviction(item)}
            <span class="evict-warn" title="Older unsaved runs are pruned as new ones arrive">
              near eviction — save it to keep it
            </span>
          {/if}
        </button>
      {/each}
    </div>
  {/if}
</div>

{#if loading}<div class="loading">Comparing…</div>{/if}

{#if comparison}
  <label class="timeline-toggle">
    <input type="checkbox" bind:checked={showTimelines} />
    Show full pipeline timelines
  </label>

  <div class="columns" style="grid-template-columns: repeat({orderedIds.length}, minmax(320px, 1fr));">
    {#each orderedIds as id (id)}
      {@const isBaseline = id === baselineId}
      {@const d = diffFor(id)}
      <div class="column" class:baseline={isBaseline}>
        <div class="col-header">
          <label class="baseline-pick">
            <input
              type="radio"
              name="baseline"
              checked={isBaseline}
              onchange={() => setBaseline(id)}
            />
            <span>{isBaseline ? 'Baseline' : 'Set as baseline'}</span>
          </label>
          <div class="col-name">{runName(id)}</div>
          <div class="col-meta">
            {new Date(exchanges[id]?.created_at || '').toLocaleString()}
          </div>
          <div class="col-actions">
            <button onclick={() => replay(id)} title="Re-run this exchange against your current configuration">
              Replay
            </button>
            <a href={`/api/debug/${id}/export?format=markdown`} onclick={(e) => { e.preventDefault(); downloadFromApi(`/api/debug/${id}/export?format=markdown`, 'run.md').catch((err) => error = err.message) }}>
              Export
            </a>
          </div>
          {#if !isBaseline}
            <div class="change-summary">
              {changeCount(d)} difference{changeCount(d) === 1 ? '' : 's'} vs baseline
            </div>
          {/if}
        </div>

        <MetricsBar
          totals={totalsFor(id)}
          baseline={isBaseline ? undefined : totalsFor(baselineId)}
          compact
        />

        {#if !isBaseline && d}
          <div class="diffs">
            {#if !d.identity?.same}
              <div class="diff-block">
                <div class="diff-title">Configuration</div>
                {#each Object.entries(d.identity.changed) as [field, v] (field)}
                  <div class="diff-row">
                    <span class="field">{field}</span>
                    <span class="was">{JSON.stringify((v as any).baseline)}</span>
                    <span class="arrow">→</span>
                    <span class="now">{JSON.stringify((v as any).other)}</span>
                  </div>
                {/each}
              </div>
            {/if}

            {#if !d.cantrips?.same}
              <div class="diff-block">
                <div class="diff-title">Cantrips</div>
                {#each d.cantrips.added as c}
                  <div class="diff-row"><span class="tag add">added</span>
                    <GitvLink type="cantrip" id={c.id} label={c.name} /></div>
                {/each}
                {#each d.cantrips.removed as c}
                  <div class="diff-row"><span class="tag remove">removed</span>
                    <GitvLink type="cantrip" id={c.id} label={c.name} /></div>
                {/each}
                {#each d.cantrips.trigger_changed as c}
                  <div class="diff-row">
                    <span class="tag change">trigger</span>
                    <GitvLink type="cantrip" id={c.id} label={c.name} />
                    <span class="muted">
                      {c.baseline_triggered ? 'fired' : 'silent'} → {c.other_triggered ? 'fired' : 'silent'}
                      {#if c.other_reason}({c.other_reason}){/if}
                    </span>
                  </div>
                {/each}
                {#each d.cantrips.code_changed as c}
                  <div class="diff-row">
                    <span class="tag change">code</span>
                    <GitvLink type="cantrip" id={c.id} label={c.name} />
                    <code class="muted">{c.baseline_hash} → {c.other_hash}</code>
                  </div>
                  {#if c.diff?.hunks?.length}
                    <details><summary>Code diff</summary>
                      <pre class="diffpre">{#each c.diff.hunks as h}{#each h.baseline as l}<span class="del">- {l}</span>{'\n'}{/each}{#each h.other as l}<span class="ins">+ {l}</span>{'\n'}{/each}{/each}</pre>
                    </details>
                  {/if}
                {/each}
                {#each d.cantrips.data_changed as c}
                  <div class="diff-row">
                    <span class="tag change">stored data</span>
                    <GitvLink type="cantrip" id={c.id} label={c.name} />
                    <span class="muted">{c.stores.join(', ')}</span>
                  </div>
                {/each}
                {#each d.cantrips.output_changed as c}
                  <div class="diff-row">
                    <span class="tag change">output</span>
                    <GitvLink type="cantrip" id={c.id} label={c.name} />
                    <span class="muted">{c.fields.join(', ')}</span>
                  </div>
                {/each}
              </div>
            {/if}

            {#each [['Lorebook entries', d.lorebooks, 'lorebook'], ['Skills', d.skills, 'skill'], ['Samples', d.samples, 'sample']] as [title, block, linkType]}
              {#if block && !block.same}
                <div class="diff-block">
                  <div class="diff-title">{title}
                    <span class="muted">{block.baseline_tokens} → {block.other_tokens} tok</span>
                  </div>
                  {#each block.added as e}
                    <div class="diff-row"><span class="tag add">added</span>
                      <GitvLink type={linkType} id={e.lorebook_id || e.id} label={e.name} />
                      <span class="muted">~{e.tokens} tok</span></div>
                  {/each}
                  {#each block.removed as e}
                    <div class="diff-row"><span class="tag remove">removed</span>
                      <GitvLink type={linkType} id={e.lorebook_id || e.id} label={e.name} />
                      <span class="muted">~{e.tokens} tok</span></div>
                  {/each}
                </div>
              {/if}
            {/each}

            {#if d.stages && !d.stages.same}
              <div class="diff-block">
                <div class="diff-title">Pipeline stages</div>
                {#each d.stages.only_in_baseline as s}
                  <div class="diff-row"><span class="tag remove">only in baseline</span> {s.label}</div>
                {/each}
                {#each d.stages.only_in_other as s}
                  <div class="diff-row"><span class="tag add">only here</span> {s.label}</div>
                {/each}
                {#each d.stages.changed as s}
                  <div class="diff-row">
                    <span class="tag change">changed</span> {s.label}
                    <span class="muted">{s.baseline_detail} → {s.other_detail}</span>
                  </div>
                {/each}
              </div>
            {/if}

            {#if d.verification?.ran && !d.verification.same}
              <div class="diff-block">
                <div class="diff-title">Verification</div>
                {#if d.verification.approval_changed}
                  <div class="diff-row">
                    <span class="tag change">result</span>
                    {d.verification.baseline_approved ? 'PASS' : 'FAIL'} →
                    <strong class:bad={!d.verification.other_approved}>
                      {d.verification.other_approved ? 'PASS' : 'FAIL'}
                    </strong>
                  </div>
                {/if}
                {#each d.verification.other_violations.filter((v: string) => !d.verification.baseline_violations.includes(v)) as v}
                  <div class="diff-row"><span class="tag remove">new violation</span> {v}</div>
                {/each}
                {#each d.verification.baseline_violations.filter((v: string) => !d.verification.other_violations.includes(v)) as v}
                  <div class="diff-row"><span class="tag add">resolved</span> {v}</div>
                {/each}
              </div>
            {/if}

            {#if d.response && !d.response.identical}
              <div class="diff-block">
                <div class="diff-title">Response
                  <span class="muted">
                    {Math.round(d.response.similarity * 100)}% similar
                    · by {d.response.granularity}
                  </span>
                </div>
                <details open><summary>Diff</summary>
                  <pre class="diffpre">{#each d.response.hunks as h}{#each h.baseline as l}<span class="del">- {l}</span>{'\n'}{/each}{#each h.other as l}<span class="ins">+ {l}</span>{'\n'}{/each}{/each}</pre>
                </details>
              </div>
            {/if}

            {#if d.reasoning && !d.reasoning.identical && !d.reasoning.baseline_empty}
              <div class="diff-block">
                <div class="diff-title">Reasoning
                  <span class="muted">
                    {Math.round(d.reasoning.similarity * 100)}% similar
                    · by {d.reasoning.granularity}
                  </span>
                </div>
                <details><summary>Diff</summary>
                  <pre class="diffpre">{#each d.reasoning.hunks as h}{#each h.baseline as l}<span class="del">- {l}</span>{'\n'}{/each}{#each h.other as l}<span class="ins">+ {l}</span>{'\n'}{/each}{/each}</pre>
                </details>
              </div>
            {/if}

            {#if changeCount(d) === 0}
              <div class="identical">No differences from the baseline.</div>
            {/if}
          </div>
        {/if}

        {#if showTimelines && exchanges[id]}
          <div class="timeline">
            <RunColumn exchange={exchanges[id]} diff={d} compact />
          </div>
        {/if}
      </div>
    {/each}
  </div>
{/if}

<style>
  .page-header { display: flex; justify-content: space-between; align-items: center; }
  .actions { display: flex; gap: 8px; }

  .picker { display: flex; flex-wrap: wrap; gap: 8px; }
  .pick {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    text-align: left;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 8px 10px;
    min-width: 210px;
    cursor: pointer;
    color: var(--text);
  }
  .pick.active { border-color: var(--accent); background: var(--surface); }
  .pick-name { font-size: 13px; display: flex; align-items: center; gap: 6px; }
  .pick-meta { font-size: 11px; color: var(--text-dim); }
  .evict-warn { font-size: 11px; color: var(--warn); }

  .timeline-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-dim);
    margin: 12px 0 8px;
  }
  .timeline-toggle input { width: auto; }

  .columns {
    display: grid;
    gap: 12px;
    align-items: start;
    overflow-x: auto;
    padding-bottom: 8px;
  }
  .column {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px;
    min-width: 0;
  }
  .column.baseline { border-color: var(--accent); }

  .col-header { margin-bottom: 10px; }
  .baseline-pick {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-dim);
    cursor: pointer;
  }
  .baseline-pick input { width: auto; }
  .col-name { font-size: 15px; margin-top: 4px; word-break: break-word; }
  .col-meta { font-size: 11px; color: var(--text-dim); }
  .col-actions { display: flex; gap: 6px; margin-top: 6px; align-items: center; }
  .col-actions a { font-size: 12px; color: var(--accent); cursor: pointer; }
  .change-summary { font-size: 12px; color: var(--text-dim); margin-top: 6px; }

  .diff-block {
    border-top: 1px solid var(--border);
    padding: 8px 0;
  }
  .diff-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-dim);
    margin-bottom: 4px;
    display: flex;
    gap: 8px;
    align-items: baseline;
  }
  .diff-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    font-size: 12px;
    padding: 2px 0;
  }
  .tag {
    font-size: 10px;
    text-transform: uppercase;
    padding: 1px 5px;
    border-radius: 3px;
  }
  .tag.add { background: var(--success); color: #000; }
  .tag.remove { background: var(--danger); color: #fff; }
  .tag.change { background: var(--warn); color: #000; }
  .field { font-family: var(--mono); }
  .was { color: var(--text-dim); text-decoration: line-through; }
  .arrow { color: var(--text-dim); }
  .now { color: var(--text); }
  .muted { color: var(--text-dim); font-size: 11px; }
  .bad { color: var(--danger); }
  .identical { font-size: 12px; color: var(--text-dim); padding: 8px 0; }

  .diffpre {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 8px;
    font-family: var(--mono);
    font-size: 11px;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 300px;
    overflow: auto;
  }
  .del { color: var(--danger); }
  .ins { color: var(--success); }

  details > summary { cursor: pointer; font-size: 11px; color: var(--text-dim); }
  .timeline { border-top: 1px solid var(--border); margin-top: 10px; padding-top: 6px; }
</style>
