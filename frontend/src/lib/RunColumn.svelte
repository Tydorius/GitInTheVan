<script lang="ts">
  /**
   * One debug run rendered as a stage timeline.
   *
   * Shared by the single-run Debug view and each column of the comparison, so
   * the two cannot drift apart. Everything a stage recorded is shown in full —
   * reasoning especially, which used to be clipped at 500 characters and is the
   * main thing a user is trying to read when comparing two runs.
   *
   * `diff` is the comparison result for this run against the baseline, when
   * there is one. It only drives highlight badges here; the diff detail itself
   * is rendered by Compare.
   */
  import type { DebugExchange } from '../api'
  import GitvLink from './GitvLink.svelte'

  export let exchange: DebugExchange
  export let diff: any = null
  export let compact = false

  let expandedStage: number | null = null

  $: pipeline = exchange?.pipeline_data || ({} as any)
  $: stages = pipeline.stages || []
  $: run = pipeline.run || {}
  $: cantrips = run.cantrips || []
  $: firedCantrips = cantrips.filter((c: any) => c.triggered)
  $: silentCantrips = cantrips.filter((c: any) => !c.triggered)
  $: verification = exchange?.verification_data || {}

  function toggleStage(idx: number) {
    expandedStage = expandedStage === idx ? null : idx
  }

  function formatMessages(jsonStr: string | null | undefined): string {
    if (!jsonStr) return ''
    try {
      const msgs = typeof jsonStr === 'string' ? JSON.parse(jsonStr) : jsonStr
      return msgs
        .map((m: any) => `[${m.role}] ${typeof m.content === 'string' ? m.content : JSON.stringify(m.content)}`)
        .join('\n\n')
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

  /** Cantrip detail lives on the run, not the stage, so stages store only ids. */
  function cantripById(id: string): any {
    return cantrips.find((c: any) => c.id === id)
  }

  /** Stages whose object differs from the baseline get a marker. */
  function stageIsDifferent(stage: any): boolean {
    if (!diff) return false
    const changed = diff.stages?.changed || []
    const onlyHere = diff.stages?.only_in_other || []
    return (
      changed.some((s: any) => s.label === stage.label) ||
      onlyHere.some((s: any) => s.label === stage.label)
    )
  }

  function ms(v: number | null | undefined): string {
    if (typeof v !== 'number') return ''
    return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`
  }

  function metaWithoutThinking(metadata: any): any {
    if (!metadata) return null
    const { thinking, ...rest } = metadata
    return Object.keys(rest).length ? rest : null
  }
</script>

{#if pipeline.truncated}
  <div class="truncation-note">
    <strong>This trace was truncated.</strong> {pipeline.truncated_reason}
  </div>
{/if}

{#if pipeline.tags?.length}
  <div class="tags">
    {#each pipeline.tags as tag}<span class="badge">{tag}</span>{/each}
  </div>
{/if}

{#if run.source === 'replay'}
  <div class="replay-note">
    Replay of run <code>{(run.replay_of || '').slice(0, 8)}</code> against the configuration
    current at the time it ran.
  </div>
{:else if run.source === 'sandbox'}
  <div class="replay-note">
    Sandbox run. This used a forked copy of the conversation, so anything it wrote
    stayed in the sandbox and the original chat was not touched.
  </div>
{/if}

<!-- Upstream calls: the endpoint and model actually used, which the stored
     "model" field does not tell you when an endpoint or failover overrides it. -->
{#if run.llm_calls?.length}
  <div class="section-title">Upstream calls</div>
  <div class="calls">
    {#each run.llm_calls as call}
      <div class="call" class:failed={call.status_code !== 200}>
        <span class="purpose">{call.purpose}{call.stage_index !== null && call.stage_index !== undefined ? ` #${call.stage_index + 1}` : ''}</span>
        <span class="model">{call.model_resolved || call.model_requested || '—'}</span>
        {#if call.endpoint_name}<span class="endpoint">{call.endpoint_name}</span>{/if}
        {#if call.failover_attempt > 0}
          <span class="badge warn-badge">failover #{call.failover_attempt}</span>
        {/if}
        <span class="timing">{ms(call.latency_ms)}</span>
        <span class="tokens">
          {call.prompt_tokens}+{call.completion_tokens}{call.tokens_source === 'estimated' ? ' est' : ''}
        </span>
        {#if call.error}<span class="err">{call.error}</span>{/if}
      </div>
    {/each}
  </div>
{/if}

<!-- Cantrips: nothing about these was recorded before Phase 22. -->
{#if cantrips.length}
  <div class="section-title">
    Cantrips
    <span class="muted">{firedCantrips.length} of {cantrips.length} ran</span>
  </div>
  <div class="cantrips">
    {#each firedCantrips as c}
      <div class="cantrip" class:errored={!!c.error}>
        <GitvLink type="cantrip" id={c.id} label={c.name} />
        <span class="muted">{c.position}</span>
        {#if c.duration_ms}<span class="timing">{ms(c.duration_ms)}</span>{/if}
        {#if c.code_hash}<code class="hash" title="Content hash of the code that ran">{c.code_hash}</code>{/if}
        {#if c.fields_changed?.length}
          <span class="muted">changed {c.fields_changed.join(', ')}</span>
        {:else}
          <span class="muted">no output</span>
        {/if}
        {#if c.data_changes && Object.keys(c.data_changes).length}
          <div class="stores">
            {#each Object.entries(c.data_changes) as [store, changes]}
              {#each Object.entries(changes) as [key, change]}
                <div class="store-change">
                  <span class="store">{store.replace('_data', '')}</span>
                  <code>{key}</code>
                  {#if change.op === 'added'}
                    <span class="muted">set to</span> <code>{JSON.stringify(change.to)}</code>
                  {:else if change.op === 'removed'}
                    <span class="muted">removed (was {JSON.stringify(change.from)})</span>
                  {:else}
                    <code>{JSON.stringify(change.from)}</code>
                    <span class="muted">→</span>
                    <code>{JSON.stringify(change.to)}</code>
                  {/if}
                </div>
              {/each}
            {/each}
          </div>
        {/if}
        {#if c.error}<div class="err">{c.error}</div>{/if}
        {#if c.debug_logs?.length}
          <details>
            <summary>{c.debug_logs.length} log line(s)</summary>
            <pre>{c.debug_logs.join('\n')}</pre>
          </details>
        {/if}
      </div>
    {/each}
    {#each silentCantrips as c}
      <div class="cantrip silent">
        <GitvLink type="cantrip" id={c.id} label={c.name} />
        <span class="muted">did not run — {c.reason || 'no reason recorded'}</span>
      </div>
    {/each}
  </div>
{/if}

<div class="section-title">Pipeline timeline</div>
{#if !stages.length}
  <div class="empty-state">No stages recorded.</div>
{/if}

{#each stages as stage, idx}
  <div class="stage" class:differs={stageIsDifferent(stage)}>
    <button class="stage-header" onclick={() => toggleStage(idx)}>
      <span class="idx">{idx + 1}</span>
      <span class="stage-label">
        {stage.label}
        {#if stage.item_id && stage.item_name}
          — <GitvLink
              type={stage.name === 'map_stage' || stage.name === 'map_stage_cantrips' ? 'map' : 'cantrip'}
              id={stage.name.startsWith('map_stage') ? stage.metadata?.map_id : stage.item_id}
              label={stage.item_name} />
        {/if}
      </span>
      {#if stageChanged(stage)}<span class="badge changed">changed</span>{/if}
      {#if stageIsDifferent(stage)}<span class="badge diff-badge">differs</span>{/if}
      <span class="chev">{expandedStage === idx ? '▼' : '▶'}</span>
    </button>

    {#if stage.detail}<div class="detail">{stage.detail}</div>{/if}

    {#if expandedStage === idx}
      <div class="stage-body">
        {#if stage.setting}
          <div class="kv"><code>{stage.setting}</code> = <code>{JSON.stringify(stage.setting_value)}</code></div>
        {/if}

        {#if stage.metadata?.entries?.length}
          <div class="sub">Lorebook entries</div>
          {#each stage.metadata.entries as e}
            <div class="entry">
              <GitvLink type="lorebook" id={e.lorebook_id} label={e.entry_name || e.entry_id}
                        extra={{ entry: e.entry_id }} />
              {#if e.lorebook_name}<span class="muted">in {e.lorebook_name}</span>{/if}
              <span class="muted">~{e.tokens} tok</span>
            </div>
          {/each}
        {/if}

        {#if stage.metadata?.skills?.length}
          <div class="sub">Skills</div>
          {#each stage.metadata.skills as s}
            <div class="entry">
              <GitvLink type="skill" id={s.id} label={s.name} />
              <span class="muted">~{s.tokens} tok</span>
            </div>
          {/each}
        {/if}

        {#if stage.metadata?.samples?.length}
          <div class="sub">Writing samples</div>
          {#each stage.metadata.samples as s}
            <div class="entry">
              <GitvLink type="sample" id={s.id} label={s.name} />
              <span class="muted">~{s.tokens} tok</span>
            </div>
          {/each}
        {/if}

        {#if stage.metadata?.ran?.length}
          <div class="sub">Cantrips that ran here</div>
          {#each stage.metadata.ran as r}
            {@const c = cantripById(r.id)}
            <div class="entry">
              <GitvLink type="cantrip" id={r.id} label={r.name} />
              {#if c?.duration_ms}<span class="muted">{ms(c.duration_ms)}</span>{/if}
            </div>
          {/each}
        {/if}

        {#if stage.metadata?.skipped?.length}
          <div class="sub">Considered but did not run</div>
          {#each stage.metadata.skipped as r}
            <div class="entry silent">
              <GitvLink type="cantrip" id={r.id} label={r.name} />
              <span class="muted">{r.reason}</span>
            </div>
          {/each}
        {/if}

        <!-- Reasoning in full. It was clipped at 500 chars before Phase 22,
             which made comparing two runs' thinking impossible. -->
        {#if stage.metadata?.thinking}
          <div class="sub">Reasoning</div>
          <pre class="reasoning">{stage.metadata.thinking}</pre>
        {/if}

        {#if metaWithoutThinking(stage.metadata)}
          <details>
            <summary>Metadata</summary>
            <pre>{JSON.stringify(metaWithoutThinking(stage.metadata), null, 2)}</pre>
          </details>
        {/if}

        {#if stage.messages_dropped}
          <div class="muted">Message snapshots were dropped to fit the trace size limit.</div>
        {:else if !compact && stage.messages_after}
          <details>
            <summary>Messages</summary>
            <div class="sub">Before</div>
            <pre>{formatMessages(stage.messages_before)}</pre>
            <div class="sub">After</div>
            <pre>{formatMessages(stage.messages_after)}</pre>
          </details>
        {/if}

        {#if stage.content_after !== undefined && stage.content_after !== ''}
          <div class="sub">Content after</div>
          <pre>{stage.content_after}</pre>
        {/if}
      </div>
    {/if}
  </div>
{/each}

{#if exchange?.response_content}
  <div class="section-title">Final response</div>
  <pre class="response">{exchange.response_content}</pre>
{/if}

{#if verification && 'approved' in verification}
  <div class="section-title">
    Verification
    <span class="badge" class:pass={verification.approved} class:fail={!verification.approved}>
      {verification.approved ? 'APPROVED' : 'REJECTED'}
    </span>
    <span class="muted">{verification.retries_used} retry/retries</span>
  </div>
  {#each verification.check_history || [] as check, i}
    <div class="check">
      <strong>Check {i + 1}: {check.approved ? 'PASS' : 'FAIL'}</strong>
      {#if check.violations?.length}
        <ul>
          {#each check.violations as v}
            <li>{typeof v === 'string' ? v : v.detail || v.reason || JSON.stringify(v)}</li>
          {/each}
        </ul>
      {/if}
      {#if check.thinking}<pre class="reasoning">{check.thinking}</pre>{/if}
    </div>
  {/each}
{/if}

<style>
  .section-title {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-dim);
    margin: 14px 0 6px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .muted { color: var(--text-dim); font-size: 12px; }
  .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }

  .truncation-note, .replay-note {
    background: var(--surface2);
    border-left: 3px solid var(--warn);
    padding: 8px 10px;
    border-radius: var(--radius);
    font-size: 12px;
    margin-bottom: 10px;
  }
  .replay-note { border-left-color: var(--accent); }

  .calls { display: flex; flex-direction: column; gap: 4px; }
  .call {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    font-size: 12px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 5px 8px;
  }
  .call.failed { border-color: var(--danger); }
  .purpose { color: var(--text-dim); text-transform: uppercase; font-size: 10px; }
  .model, .hash { font-family: var(--mono); }
  .endpoint { color: var(--text-dim); }
  .timing, .tokens { font-family: var(--mono); color: var(--text-dim); margin-left: auto; }
  .tokens { margin-left: 0; }
  .err { color: var(--danger); font-size: 12px; }

  .cantrips { display: flex; flex-direction: column; gap: 4px; }
  .cantrip {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    font-size: 13px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 5px 8px;
  }
  .cantrip.errored { border-color: var(--danger); }
  .cantrip.silent, .entry.silent { opacity: 0.65; }
  .hash { font-size: 11px; color: var(--text-dim); }
  .stores { flex-basis: 100%; margin-top: 4px; }
  .store-change {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
    font-size: 11px;
    padding: 1px 0;
  }
  .store {
    font-size: 10px;
    text-transform: uppercase;
    color: var(--text-dim);
    min-width: 56px;
  }

  .stage { border-bottom: 1px solid var(--border); padding: 4px 0; }
  .stage.differs { border-left: 3px solid var(--accent); padding-left: 8px; }
  .stage-header {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    background: none;
    border: none;
    text-align: left;
    padding: 6px 0;
    color: var(--text);
    cursor: pointer;
  }
  .idx {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    min-width: 18px;
  }
  .stage-label { flex: 1; }
  .chev { color: var(--text-dim); font-size: 10px; }
  .detail { font-size: 12px; color: var(--text-dim); padding-left: 26px; }
  .stage-body { padding: 8px 0 8px 26px; }
  .sub {
    font-size: 11px;
    text-transform: uppercase;
    color: var(--text-dim);
    margin: 8px 0 4px;
  }
  .kv, .entry {
    font-size: 12px;
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    padding: 2px 0;
  }

  pre {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 8px;
    font-family: var(--mono);
    font-size: 12px;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 340px;
    overflow: auto;
    margin: 0 0 6px;
  }
  .reasoning { border-left: 3px solid var(--accent); }
  .response { max-height: none; }

  .badge.changed { background: var(--surface2); color: var(--text-dim); }
  .badge.diff-badge { background: var(--accent); color: #fff; }
  .badge.warn-badge { background: var(--warn); color: #000; }
  .badge.pass { background: var(--success); color: #000; }
  .badge.fail { background: var(--danger); color: #fff; }

  .check { font-size: 13px; margin-bottom: 8px; }
  details > summary { cursor: pointer; font-size: 12px; color: var(--text-dim); }
</style>
