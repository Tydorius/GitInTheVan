<script lang="ts">
  // Always mounted (see App.svelte); collapses to a slim rail when not in
  // use so it costs nothing idle. Written in the codebase's Svelte-4 idiom
  // (export let, $: reactives) with Svelte 5 event attribute syntax -- see
  // ParameterEditor.svelte's header comment.

  import { onDestroy } from 'svelte'
  import {
    paneExpanded, paneWidth, setPaneWidth, assistantConfig,
    conversations, active, pending, busy, lastError, notice, capHit,
    refreshConversations, openConversation, newConversation, sendMessage,
    continueTurn, decide, abort, toggleSaved, deleteConversation,
    forkConversation, exportConversation, setYolo,
  } from './store'
  import { renderMarkdown } from './markdown'
  import ConfirmCard from './ConfirmCard.svelte'

  let draft = ''
  let menuOpen = false
  let expandedItems: Record<string, boolean> = {}
  let dragging = false

  function toggleItem(id: string) {
    expandedItems = { ...expandedItems, [id]: !expandedItems[id] }
  }

  function argsSummary(args: any): string {
    if (!args || typeof args !== 'object') return ''
    const text = JSON.stringify(args)
    return text.length > 90 ? `${text.slice(0, 90)}…` : text
  }

  function resultText(result: any): string {
    return typeof result === 'string' ? result : JSON.stringify(result, null, 2)
  }

  function onPick(e: Event) {
    const id = (e.currentTarget as HTMLSelectElement).value
    if (id) openConversation(id)
  }

  function onComposerKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  function send() {
    const text = draft.trim()
    if (!text || $busy || $pending) return
    draft = ''
    sendMessage(text)
  }

  function onDeleteClick() {
    menuOpen = false
    const conv = $active
    if (!conv) return
    if (!confirm(`Delete conversation "${conv.title || 'Untitled'}"? This cannot be undone.`)) return
    deleteConversation(conv.id)
  }

  function onDrag(e: MouseEvent) {
    if (!dragging) return
    setPaneWidth(window.innerWidth - e.clientX)
  }

  function stopDrag() {
    dragging = false
    window.removeEventListener('mousemove', onDrag)
    window.removeEventListener('mouseup', stopDrag)
  }

  function startDrag(e: MouseEvent) {
    dragging = true
    e.preventDefault()
    window.addEventListener('mousemove', onDrag)
    window.addEventListener('mouseup', stopDrag)
  }

  onDestroy(stopDrag)

  function openPane() {
    paneExpanded.set(true)
    refreshConversations()
  }

  $: if ($paneExpanded) refreshConversations()
</script>

<div class="assistant-pane" class:expanded={$paneExpanded} style={`--assistant-width: ${$paneWidth}px`}>
  {#if !$paneExpanded}
    <button class="assistant-rail" onclick={openPane} title="Open assistant" aria-label="Open assistant">
      <span class="rail-icon">✦</span>
      {#if $pending || $busy}<span class="rail-badge"></span>{/if}
    </button>
  {:else}
    <div class="assistant-drag" role="separator" aria-orientation="vertical" onmousedown={startDrag}></div>
    <div class="assistant-body">
      <div class="assistant-header">
        <select class="assistant-picker" value={$active?.id || ''} onchange={onPick} aria-label="Conversation">
          <option value="" disabled>Select a conversation…</option>
          {#each $conversations as c (c.id)}
            <option value={c.id}>{c.saved ? '★ ' : ''}{c.title || 'Untitled'}</option>
          {/each}
        </select>
        <button title="New conversation" onclick={() => newConversation()}>+</button>
        <button title={$active?.saved ? 'Unsave' : 'Save'} onclick={() => toggleSaved()} disabled={!$active}>
          {$active?.saved ? '★' : '☆'}
        </button>
        <div class="assistant-menu-wrap">
          <button title="More" onclick={() => (menuOpen = !menuOpen)}>⋯</button>
          {#if menuOpen}
            <div class="assistant-menu">
              <button onclick={() => { exportConversation('json'); menuOpen = false }} disabled={!$active}>Download JSON</button>
              <button onclick={() => { exportConversation('markdown'); menuOpen = false }} disabled={!$active}>Download Markdown</button>
              <button onclick={() => { forkConversation(); menuOpen = false }} disabled={!$active}>New from summary</button>
              <button class="danger" onclick={onDeleteClick} disabled={!$active}>Delete</button>
            </div>
          {/if}
        </div>
        <button title="Collapse" aria-label="Collapse assistant" onclick={() => paneExpanded.set(false)}>»</button>
      </div>

      <div class="assistant-subhead">
        <label class="yolo-toggle" title="Content tools run without asking. Configuration tools still ask.">
          <input
            type="checkbox"
            checked={$active?.yolo || false}
            disabled={!$active}
            onchange={(e) => setYolo((e.currentTarget as HTMLInputElement).checked)}
          />
          <span class="yolo-label">Yolo</span>
        </label>
        {#if $active}
          <span class="assistant-usage">
            {$active.usage.prompt_tokens}/{$active.usage.completion_tokens} tokens · {$active.usage.tool_calls} tool calls
          </span>
        {/if}
      </div>

      {#if $notice}
        <div class="assistant-notice">{$notice}</div>
      {/if}
      {#if $lastError}
        <div class="assistant-error">
          <span>{$lastError}</span>
          <button onclick={() => lastError.set('')} aria-label="Dismiss error">&times;</button>
        </div>
      {/if}

      {#if !$assistantConfig?.endpoint_id}
        <div class="assistant-empty">
          No assistant endpoint is configured yet. <a href="#/settings">Choose one in Settings</a>.
        </div>
      {:else}
        <div class="assistant-transcript">
          {#if !$active}
            <div class="assistant-empty">Start a new conversation, or pick one above.</div>
          {:else if !$active.items.length}
            <div class="assistant-empty">
              Ask about anything in GitInTheVan &mdash; cantrips, lorebooks, verification, why something did or didn't fire.
            </div>
          {/if}

          {#each ($active?.items || []) as item (item.id)}
            {#if item.kind === 'compacted_divider'}
              <div class="context-divider"><span>Context compacted</span></div>
            {:else if item.kind === 'user'}
              <div class="bubble bubble-user">{item.content}</div>
            {:else if item.kind === 'assistant'}
              <div class="bubble bubble-assistant">
                {#if item.reasoning_content}
                  <details class="reasoning">
                    <summary>Reasoning</summary>
                    <div>{item.reasoning_content}</div>
                  </details>
                {/if}
                {@html renderMarkdown(item.content || '')}
              </div>
            {:else if item.kind === 'system_note'}
              <div class="system-note">{item.content}</div>
            {:else if item.kind === 'tool_call'}
              <div class="tool-row">
                <button class="tool-row-head" onclick={() => toggleItem(item.id)}>
                  <span class="tool-chevron">{expandedItems[item.id] ? '▼' : '▶'}</span>
                  <span class="tool-name">{item.name}</span>
                  <span class="tool-summary">{argsSummary(item.args)}</span>
                </button>
                {#if expandedItems[item.id]}
                  <pre class="tool-detail">{JSON.stringify(item.args, null, 2)}</pre>
                {/if}
              </div>
            {:else if item.kind === 'tool_result'}
              <div class="tool-row tool-result" class:tool-error={item.ok === false}>
                <button class="tool-row-head" onclick={() => toggleItem(item.id)}>
                  <span class="tool-chevron">{expandedItems[item.id] ? '▼' : '▶'}</span>
                  <span class="tool-status">{item.ok === false ? 'error' : 'ok'}</span>
                  {#if item.duration_ms != null}<span class="tool-duration">{item.duration_ms}ms</span>{/if}
                  {#if item.truncated}<span class="tool-truncated">truncated</span>{/if}
                </button>
                {#if expandedItems[item.id]}
                  <pre class="tool-detail">{resultText(item.result)}</pre>
                {/if}
              </div>
            {/if}
          {/each}

          {#if $pending}
            <ConfirmCard pending={$pending} onDecide={decide} />
          {/if}

          {#if $busy}
            <div class="assistant-busy">Working…</div>
          {/if}
        </div>

        {#if $capHit}
          <button class="assistant-continue primary" onclick={() => continueTurn()}>Continue</button>
        {/if}

        <div class="assistant-composer">
          <textarea
            bind:value={draft}
            placeholder="Ask the assistant…"
            disabled={$busy || !!$pending}
            onkeydown={onComposerKeydown}
          ></textarea>
          {#if $busy}
            <button class="danger" onclick={() => abort()}>Stop</button>
          {:else}
            <button class="primary" onclick={send} disabled={!draft.trim() || !!$pending}>Send</button>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</div>

{#if !$paneExpanded}
  <button class="assistant-mobile-toggle" onclick={openPane} title="Open assistant" aria-label="Open assistant">
    ✦
    {#if $pending || $busy}<span class="rail-badge"></span>{/if}
  </button>
{/if}
