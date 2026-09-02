<script lang="ts">
  // Human-in-the-loop confirmation for an `awaiting_confirmation` tool call.
  //
  // A field is shown as a line diff (rather than plain text) when it is long
  // enough that a diff reads better than a wall of text, or when the same key
  // exists on `pending.current` -- meaning this call would change something
  // that already has a value, which is exactly the case a diff clarifies.

  import type { PendingConfirm } from './store'
  import { lineDiff, diffStats } from './diff'

  export let pending: PendingConfirm
  export let onDecide: (decision: 'approve' | 'allow_session' | 'reject', note?: string) => void

  let note = ''
  let rejecting = false

  const RISK_LABEL: Record<string, string> = {
    read: 'Read',
    write: 'Write',
    destructive: 'Destructive',
    external_cost: 'Costs tokens',
  }

  function riskClass(risk: string): string {
    return `risk-badge risk-${risk || 'read'}`
  }

  function isDiffField(key: string, value: unknown): boolean {
    if (typeof value !== 'string') return false
    if (value.length > 200) return true
    return !!pending.current && Object.prototype.hasOwnProperty.call(pending.current, key)
  }

  function display(value: unknown): string {
    if (value === null || value === undefined) return ''
    if (typeof value === 'string') return value
    return JSON.stringify(value, null, 2)
  }

  $: argEntries = (pending.args && typeof pending.args === 'object' ? Object.entries(pending.args) : []) as [string, unknown][]

  function submit(decision: 'approve' | 'allow_session' | 'reject') {
    onDecide(decision, decision === 'reject' ? note.trim() || undefined : undefined)
    note = ''
    rejecting = false
  }
</script>

<div class="confirm-card">
  <div class="confirm-head">
    <span class={riskClass(pending.risk)}>{RISK_LABEL[pending.risk] || pending.risk}</span>
    <strong class="confirm-tool">{pending.name}</strong>
    <a class="confirm-group" href={`#${pending.page}`} target="_blank" rel="noopener">{pending.group}</a>
  </div>

  {#if pending.summary}
    <p class="confirm-summary">{pending.summary}</p>
  {/if}

  {#if argEntries.length}
    <table class="confirm-args">
      <tbody>
        {#each argEntries as [key, value] (key)}
          <tr>
            <td class="confirm-arg-key">{key}</td>
            <td class="confirm-arg-value">
              {#if isDiffField(key, value)}
                {@const diff = lineDiff(display(pending.current?.[key] ?? ''), display(value))}
                {@const stats = diffStats(diff)}
                <div class="diff-stats">+{stats.added} / -{stats.removed}</div>
                <pre class="diff-block">{#each diff as line, i (i)}<div class="diff-line diff-{line.kind}">{line.kind === 'add' ? '+ ' : line.kind === 'del' ? '- ' : '  '}{line.text}</div>{/each}</pre>
              {:else}
                <span class="confirm-arg-plain">{display(value)}</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <div class="confirm-actions">
    <button class="primary" onclick={() => submit('approve')}>Approve once</button>
    {#if pending.can_allow_session}
      <button onclick={() => submit('allow_session')}>Allow for session</button>
    {/if}
    <button class="danger" onclick={() => (rejecting = !rejecting)}>Reject</button>
  </div>

  {#if rejecting}
    <div class="confirm-reject">
      <input type="text" placeholder="Optional note (helps the model correct itself)" bind:value={note} />
      <button class="danger" onclick={() => submit('reject')}>Confirm reject</button>
    </div>
  {/if}
</div>

<style>
  .confirm-card {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface2);
    padding: 10px;
    font-size: 12px;
  }
  .confirm-head {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 6px;
  }
  .confirm-tool {
    font-family: var(--mono);
  }
  .confirm-group {
    font-size: 11px;
  }
  .confirm-summary {
    color: var(--text-dim);
    font-size: 11px;
    margin: 4px 0 8px;
  }
  .confirm-args {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 8px;
  }
  .confirm-args td {
    padding: 3px 6px 3px 0;
    border-bottom: none;
    vertical-align: top;
  }
  .confirm-arg-key {
    color: var(--text-dim);
    font-family: var(--mono);
    font-size: 11px;
    white-space: nowrap;
  }
  .confirm-arg-value {
    font-size: 11px;
    word-break: break-word;
  }
  .confirm-arg-plain {
    font-family: var(--mono);
  }
  .confirm-actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .confirm-reject {
    display: flex;
    gap: 6px;
    margin-top: 8px;
  }
  .confirm-reject input {
    flex: 1;
  }
</style>
