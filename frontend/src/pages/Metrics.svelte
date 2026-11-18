<script lang="ts">
  /**
   * Historical token metrics: which prompt objects bloat the budget over
   * time, and what each LLM role costs in latency and tokens.
   *
   * Complements the Debug page: Debug shows one run, this shows the trend.
   * Token counts from upstream `usage` are exact for that provider; the rest
   * are chars/4 estimates -- the split is shown rather than blended.
   */
  import { onMount } from 'svelte'
  import { api } from '../api'
  import type {
    LlmCallAggregate, LlmCallMetricRow, MetricsSummary, PromptObjectAggregate, PromptObjectMetricRow,
  } from '../api'
  import CollapsibleCard from '../lib/CollapsibleCard.svelte'
  import { CollapseController } from '../lib/collapse'

  let collapse = new CollapseController('metrics', ['prompt-objects', 'llm-calls', 'recent-rows'])

  let summary: MetricsSummary | null = null
  let promptRows: PromptObjectMetricRow[] = []
  let llmRows: LlmCallMetricRow[] = []
  let loading = true
  let error = ''

  let days = 30
  let chatId = ''
  let includeFlagged = false

  const PURPOSE_LABELS: Record<string, string> = {
    main: 'Driver',
    verification_judge: 'Navigator (judge)',
    summarizer: 'Summarizer',
    map_stage: 'Map Stage',
    assistant: 'Assistant',
  }

  const TYPE_LABELS: Record<string, string> = {
    lorebook_entry: 'Lorebook entry',
    cantrip: 'Cantrip',
    cantrip_tool: 'Tool result',
    summary_conversation: 'Conversation summary',
    summary_scenario: 'Scenario summary',
    memory_injection: 'Memory',
    skill: 'Skill',
    sample: 'Writing sample',
    stage_instructions: 'Stage instructions',
  }

  function purposeLabel(p: string): string { return PURPOSE_LABELS[p] || p }
  function typeLabel(t: string): string { return TYPE_LABELS[t] || t }

  function shortChat(id: string): string { return id.length > 12 ? `${id.slice(0, 12)}…` : id }

  function signed(n: number): string {
    return n > 0 ? `+${n.toLocaleString()}` : n.toLocaleString()
  }

  async function load() {
    loading = true
    error = ''
    try {
      summary = await api.getMetricsSummary(days, chatId, includeFlagged)
      const [p, l] = await Promise.all([
        api.listPromptObjectMetrics(20, 0, chatId),
        api.listLlmCallMetrics(20, 0, chatId),
      ])
      promptRows = p.rows
      llmRows = l.rows
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  function applyFilters() { load() }

  $: promptObjects = summary?.prompt_objects ?? []
  $: typeTotals = summary?.prompt_type_totals ?? []
  $: llmCalls = summary?.llm_calls ?? []

  onMount(load)
</script>

<div class="metrics-header">
  <h2>Metrics</h2>
  <div class="filters">
    <label for="metrics-window">Window</label>
    <select id="metrics-window" bind:value={days} onchange={applyFilters}>
      <option value={7}>7 days</option>
      <option value={30}>30 days</option>
      <option value={90}>90 days</option>
    </select>
    <label for="metrics-chat">Chat</label>
    <select id="metrics-chat" bind:value={chatId} onchange={applyFilters}>
      <option value="">All chats</option>
      {#each summary?.chats ?? [] as c (c)}
        <option value={c}>{shortChat(c)}</option>
      {/each}
    </select>
    <label for="metrics-flagged" class="checkbox-label">
      <input id="metrics-flagged" type="checkbox" bind:checked={includeFlagged} onchange={applyFilters} />
      Include sandbox/replay
    </label>
    <button onclick={load}>Refresh</button>
  </div>
</div>

{#if error}
  <div class="empty-state">{error}</div>
{/if}

{#if loading && !summary}
  <div class="loading">Loading...</div>
{:else}
  <CollapsibleCard title="Prompt Objects" cardKey="prompt-objects" {collapse}>
    <p class="hint">
      What each pipeline stage added to (or removed from) the prompt, per fire.
      Estimates are chars/4. Sorted by impact.
    </p>
    {#if promptObjects.length === 0}
      <div class="empty-state">
        Nothing recorded in this window yet. Send a message through the proxy with a
        lorebook, cantrip or memory enabled.
      </div>
    {:else}
      <div class="type-totals">
        {#each typeTotals as t (t.object_type)}
          <span class="badge">{typeLabel(t.object_type)}: {signed(t.sum_difference)} tok · {t.fires} fire{t.fires === 1 ? '' : 's'}</span>
        {/each}
      </div>
      <table>
        <thead>
          <tr>
            <th>Object</th>
            <th>Type</th>
            <th>Fires</th>
            <th>Sum Δ</th>
            <th>Avg Δ</th>
          </tr>
        </thead>
        <tbody>
          {#each promptObjects as o (o.object_type + o.object_id + o.parent_id)}
            <tr>
              <td title={o.object_id}>{o.object_name || o.object_id}
                {#if o.parent_name || o.parent_id}
                  <span class="hint"> in {o.parent_name || shortChat(o.parent_id)}</span>
                {/if}
              </td>
              <td>{typeLabel(o.object_type)}</td>
              <td>{o.fires}</td>
              <td class:positive={o.sum_difference > 0} class:negative={o.sum_difference < 0}>
                {signed(o.sum_difference)}
              </td>
              <td class:positive={o.avg_difference > 0} class:negative={o.avg_difference < 0}>
                {signed(Math.round(o.avg_difference))}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </CollapsibleCard>

  <CollapsibleCard title="LLM Calls" cardKey="llm-calls" {collapse}>
    <p class="hint">
      Cost, latency and reliability per role, endpoint and model. Judge rows carry the rule.
      Tokens are only summed for calls that reported a source, split usage vs estimate.
    </p>
    {#if llmCalls.length === 0}
      <div class="empty-state">No LLM calls recorded in this window yet.</div>
    {:else}
      <table>
        <thead>
          <tr>
            <th>Role</th>
            <th>Endpoint / Model</th>
            <th>Calls</th>
            <th>OK</th>
            <th>Retries</th>
            <th>Failovers</th>
            <th>Latency (med)</th>
            <th>Prompt tok</th>
            <th>Completion tok</th>
          </tr>
        </thead>
        <tbody>
          {#each llmCalls as g (g.purpose + g.endpoint_id + g.model + g.rule_id)}
            <tr>
              <td>{purposeLabel(g.purpose)}
                {#if g.rule_name}<span class="hint"> {g.rule_name}</span>{/if}
              </td>
              <td title={g.endpoint_id}>{g.endpoint_name || g.endpoint_id || '—'}<br />
                <span class="hint">{g.model || '—'}</span></td>
              <td>{g.calls}</td>
              <td>{g.calls ? Math.round((g.ok / g.calls) * 100) : 0}%</td>
              <td>{g.retries}</td>
              <td>{g.failovers}</td>
              <td>{(g.median_latency_ms / 1000).toFixed(1)}s</td>
              <td>
                {g.prompt_tokens.toLocaleString()}
                {#if g.tokens_source_counts.usage && g.tokens_source_counts.estimate}
                  <span class="hint" title="{g.tokens_source_counts.usage} usage · {g.tokens_source_counts.estimate} estimate"> (mixed)</span>
                {:else if g.tokens_source_counts.estimate && !g.tokens_source_counts.usage}
                  <span class="hint"> est</span>
                {/if}
              </td>
              <td>{g.completion_tokens.toLocaleString()}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </CollapsibleCard>

  <CollapsibleCard title="Recent Rows" cardKey="recent-rows" {collapse}>
    <p class="hint">The raw last 20 rows of each table (this chat filter, any context).</p>
    {#if llmRows.length}
      <h4>LLM calls</h4>
      <table>
        <thead>
          <tr><th>When</th><th>Role</th><th>Model</th><th>Status</th><th>Latency</th><th>Tokens</th><th>Chat</th></tr>
        </thead>
        <tbody>
          {#each llmRows as r (r.id)}
            <tr>
              <td>{new Date(r.created_at).toLocaleString()}</td>
              <td>{purposeLabel(r.purpose)}{#if r.is_retry} <span class="badge">retry</span>{/if}</td>
              <td>{r.model || '—'}</td>
              <td>{r.status_code ?? '—'}</td>
              <td>{(r.latency_ms / 1000).toFixed(1)}s</td>
              <td>
                {#if r.tokens_source}
                  {(r.prompt_tokens ?? 0).toLocaleString()} / {(r.completion_tokens ?? 0).toLocaleString()}
                  <span class="hint">{r.tokens_source === 'usage' ? '' : 'est'}</span>
                {:else}—{/if}
              </td>
              <td title={r.chat_id}>{r.context !== 'live' ? `${r.context}:` : ''}{shortChat(r.chat_id)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
    {#if promptRows.length}
      <h4>Prompt objects</h4>
      <table>
        <thead>
          <tr><th>When</th><th>Object</th><th>Type</th><th>Δ tokens</th><th>Chat</th></tr>
        </thead>
        <tbody>
          {#each promptRows as r (r.id)}
            <tr>
              <td>{new Date(r.created_at).toLocaleString()}</td>
              <td title={r.object_id}>{r.object_id}</td>
              <td>{typeLabel(r.object_type)}</td>
              <td class:positive={r.difference > 0} class:negative={r.difference < 0}>{signed(r.difference)}</td>
              <td title={r.chat_id}>{r.context !== 'live' ? `${r.context}:` : ''}{shortChat(r.chat_id)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
    {#if !llmRows.length && !promptRows.length}
      <div class="empty-state">No rows yet.</div>
    {/if}
  </CollapsibleCard>
{/if}

<style>
  .metrics-header {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
  }
  .metrics-header h2 { margin: 0; }
  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .filters label { font-size: 13px; }
  .checkbox-label { display: flex; align-items: center; gap: 4px; }
  .type-totals {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 8px 0;
  }
  .positive { color: #d97706; }
  .negative { color: #16a34a; }
  h4 { margin: 14px 0 6px 0; }
</style>
