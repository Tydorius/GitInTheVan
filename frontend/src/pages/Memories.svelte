<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let memories: any[] = []
  let summaries: any[] = []
  let loading = true
  let error = ''
  let filterConversation = ''
  let editingId: string | null = null
  let editValue = ''
  let expandedSummary: string | null = null

  async function load() {
    loading = true
    try {
      const [memData, sumData] = await Promise.all([
        api.listMemories(filterConversation || undefined),
        api.listSummaries(),
      ])
      memories = memData.memories
      summaries = sumData.summaries
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  async function handleSave() {
    if (!editingId) return
    try {
      await api.updateMemory(editingId, editValue)
      editingId = null
      await load()
    } catch (e: any) { error = e.message }
  }

  async function handleDelete(id: string) {
    if (!confirm('Delete this memory?')) return
    try { await api.deleteMemory(id); await load() }
    catch (e: any) { error = e.message }
  }

  async function handleDeleteSummary(id: string) {
    if (!confirm('Delete this conversation summary? The conversation will be re-summarized next time it exceeds the threshold.')) return
    try { await api.deleteSummary(id); await load() }
    catch (e: any) { error = e.message }
  }

  function startEdit(m: any) {
    editingId = m.id
    editValue = m.value
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Memories</h2>
  <button onclick={load}>Refresh</button>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

<div class="card">
  <div class="form-group">
    <label for="mem-filter">Filter by Conversation ID</label>
    <div style="display: flex; gap: 8px;">
      <input id="mem-filter" bind:value={filterConversation} placeholder="Conversation ID" />
      <button onclick={load}>Filter</button>
      <button onclick={() => { filterConversation = ''; load(); }}>Clear</button>
    </div>
  </div>
</div>

{#if loading}<div class="loading">Loading...</div>
{:else if memories.length === 0}
  <div class="empty-state">
    No memories stored yet. Memories are created when the LLM includes
    <code style="color: var(--accent);">&lt;memstore key="..."&gt;value&lt;/memstore&gt;</code>
    tags in its responses.
  </div>
{:else}
  <table>
    <thead>
      <tr><th>Key</th><th>Value</th><th>Conversation</th><th>Updated</th><th>Actions</th></tr>
    </thead>
    <tbody>
      {#each memories as m}
        <tr>
          <td><strong>{m.key}</strong></td>
          <td>
            {#if editingId === m.id}
              <input bind:value={editValue} style="width: 100%;" />
            {:else}
              <span style="font-size: 12px;">{m.value}</span>
            {/if}
          </td>
          <td style="font-size: 11px; color: var(--text-dim);">{m.conversation_id ? m.conversation_id.slice(0, 12) + '...' : '—'}</td>
          <td style="font-size: 11px; color: var(--text-dim);">{new Date(m.updated_at || m.created_at).toLocaleString()}</td>
          <td>
            {#if editingId === m.id}
              <button class="primary" onclick={handleSave} style="font-size: 12px;">Save</button>
              <button onclick={() => editingId = null} style="font-size: 12px;">Cancel</button>
            {:else}
              <button onclick={() => startEdit(m)} style="font-size: 12px;">Edit</button>
              <button class="danger" onclick={() => handleDelete(m.id)} style="font-size: 12px;">Delete</button>
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

<div class="card">
  <h3>Conversation Summaries</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    Auto-generated summaries of older conversation history, used to compress long conversations. Configure thresholds in Settings.
  </p>
  {#if summaries.length === 0}
    <div class="empty-state" style="padding: 16px;">
      No conversation summaries stored yet. Summaries are generated automatically when summarization is enabled and a conversation exceeds the token threshold.
    </div>
  {:else}
    <table>
      <thead>
        <tr><th>Chat</th><th>Messages</th><th>Tokens</th><th>Updated</th><th>Actions</th></tr>
      </thead>
      <tbody>
        {#each summaries as s}
          <tr>
            <td style="font-size: 11px; color: var(--text-dim);">{s.internal_chat_id ? s.internal_chat_id.slice(0, 12) + '...' : '—'}</td>
            <td style="font-size: 12px;">{s.message_count}</td>
            <td style="font-size: 12px;">~{s.token_estimate}</td>
            <td style="font-size: 11px; color: var(--text-dim);">{new Date(s.updated_at).toLocaleString()}</td>
            <td>
              <button onclick={() => expandedSummary = expandedSummary === s.id ? null : s.id} style="font-size: 12px;">{expandedSummary === s.id ? 'Hide' : 'View'}</button>
              <button class="danger" onclick={() => handleDeleteSummary(s.id)} style="font-size: 12px;">Delete</button>
            </td>
          </tr>
          {#if expandedSummary === s.id}
            <tr>
              <td colspan="5" style="background: var(--bg-elevated);">
                <div style="white-space: pre-wrap; font-size: 12px; line-height: 1.6; padding: 8px; max-height: 300px; overflow-y: auto;">{s.summary}</div>
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<div class="card">
  <h3>How Memory Works</h3>
  <div style="color: var(--text-dim); font-size: 12px; line-height: 1.8; margin-top: 8px;">
    <p>The LLM can store persistent memories by including special tags in its responses:</p>
    <p><code style="color: var(--accent);">&lt;memstore key="location"&gt;Tavern&lt;/memstore&gt;</code></p>
    <p>These tags are automatically extracted, saved to the database, and stripped from the response before it reaches the user.</p>
    <p>On the next message, all stored memories for that conversation are injected as a <code style="color: var(--accent);">[PERSISTENT MEMORY]</code> system context block.</p>
    <p>This does NOT depend on zero-width characters or LLM cooperation for persistence — the database is the source of truth.</p>
  </div>
</div>
