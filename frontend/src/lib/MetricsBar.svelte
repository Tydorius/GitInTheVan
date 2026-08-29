<script lang="ts">
  /**
   * Token, latency and efficiency tiles for one run.
   *
   * Shared by the single-run Debug view and every column of the comparison, so
   * the same numbers are computed and labelled identically in both places.
   *
   * When `baseline` is supplied, each tile also shows the delta against it and
   * colours by direction — but only when the two runs' token sources agree.
   * Comparing an estimate to a measurement and printing a crisp percentage
   * would imply precision that is not there.
   */
  import type { DebugRunTotals } from '../api'

  export let totals: Partial<DebugRunTotals> | undefined = undefined
  export let baseline: Partial<DebugRunTotals> | undefined = undefined
  export let compact = false

  interface Tile {
    key: keyof DebugRunTotals
    label: string
    format: (v: number) => string
    lowerIsBetter: boolean
    hint: string
  }

  const ms = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`)
  const num = (v: number) => v.toLocaleString()

  const TILES: Tile[] = [
    { key: 'total_tokens', label: 'Tokens', format: num, lowerIsBetter: true,
      hint: 'Prompt plus completion across every upstream call in this run.' },
    { key: 'prompt_tokens', label: 'Prompt', format: num, lowerIsBetter: true,
      hint: 'Tokens sent upstream.' },
    { key: 'completion_tokens', label: 'Completion', format: num, lowerIsBetter: false,
      hint: 'Tokens generated.' },
    { key: 'injected_tokens', label: 'Injected', format: num, lowerIsBetter: true,
      hint: 'Tokens the pipeline added to the prompt: lorebooks, skills, memory, samples.' },
    { key: 'total_latency_ms', label: 'Total time', format: ms, lowerIsBetter: true,
      hint: 'Wall clock for the whole request.' },
    { key: 'overhead_ms', label: 'Pipeline overhead', format: ms, lowerIsBetter: true,
      hint: 'Total time minus time spent waiting on upstreams — what GitInTheVan itself cost.' },
    { key: 'tokens_per_second', label: 'Tokens/sec', format: (v) => v.toFixed(1), lowerIsBetter: false,
      hint: 'Completion tokens divided by upstream time.' },
    { key: 'llm_call_count', label: 'LLM calls', format: num, lowerIsBetter: true,
      hint: 'Upstream requests, including map stages, verification judges and failover retries.' },
  ]

  $: t = totals || {}
  $: source = t.tokens_source || ''
  // Only comparable when both sides measured the same way.
  $: comparable =
    !baseline || !baseline.tokens_source || !source || baseline.tokens_source === source
  $: visible = TILES.filter((tile) => {
    const v = t[tile.key]
    return typeof v === 'number' && !(compact && ['prompt_tokens', 'completion_tokens', 'llm_call_count'].includes(tile.key))
  })

  function delta(tile: Tile): { text: string; good: boolean | null } | null {
    if (!baseline) return null
    const a = baseline[tile.key]
    const b = t[tile.key]
    if (typeof a !== 'number' || typeof b !== 'number') return null
    const d = b - a
    if (d === 0) return { text: '=', good: null }

    const isTokenMetric = tile.key.includes('token')
    const pct = a ? ` (${d > 0 ? '+' : ''}${Math.round((d / a) * 100)}%)` : ''
    const sign = d > 0 ? '+' : ''
    const text = `${sign}${tile.format(Math.abs(d) === d ? d : d)}${pct}`

    // A delta between mixed sources is indicative only; withhold the verdict.
    if (isTokenMetric && !comparable) return { text, good: null }
    return { text, good: tile.lowerIsBetter ? d < 0 : d > 0 }
  }
</script>

{#if visible.length}
  <div class="metrics">
    {#each visible as tile (tile.key)}
      {@const d = delta(tile)}
      <div class="tile" title={tile.hint}>
        <div class="label">{tile.label}</div>
        <div class="value">{tile.format(t[tile.key] as number)}</div>
        {#if d}
          <div class="delta" class:good={d.good === true} class:bad={d.good === false}>
            {d.text}
          </div>
        {/if}
      </div>
    {/each}
  </div>

  {#if source === 'estimated'}
    <div class="note">
      Token counts are <strong>estimated</strong> — this endpoint returned no usage data.
    </div>
  {:else if source === 'mixed'}
    <div class="note">
      Token counts are <strong>mixed</strong>: some calls reported usage, some were estimated.
    </div>
  {/if}
  {#if baseline && !comparable}
    <div class="note warn">
      This run and the baseline count tokens differently
      ({baseline.tokens_source || 'unknown'} vs {source || 'unknown'}), so token deltas are
      indicative rather than exact.
    </div>
  {/if}
{:else}
  <div class="note">
    No metrics recorded for this run. Runs captured before this feature shipped have no
    token or timing data.
  </div>
{/if}

<style>
  .metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 8px;
  }
  .tile {
    flex: 1 1 auto;
    min-width: 92px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 8px 10px;
  }
  .label {
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .value {
    font-size: 17px;
    font-family: var(--mono);
    margin-top: 2px;
  }
  .delta {
    font-size: 11px;
    font-family: var(--mono);
    color: var(--text-dim);
    margin-top: 2px;
  }
  .delta.good { color: var(--success); }
  .delta.bad { color: var(--warn); }
  .note {
    font-size: 12px;
    color: var(--text-dim);
    margin-bottom: 8px;
  }
  .note.warn { color: var(--warn); }
</style>
