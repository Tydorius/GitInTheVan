<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let memories: any[] = []
  let summaries: any[] = []
  let memoryRules: any[] = []
  let loading = true
  let error = ''
  let filterConversation = ''
  let editingId: string | null = null
  let editValue = ''
  let expandedSummary: string | null = null
  let showRuleForm = false
  let editingRuleId: string | null = null
  let ruleForm = {
    name: '', description: '',
    summarization_enabled: true,
    token_threshold: 0, keep_recent: 0,
    prompt: '', tag: '', execution_order: 10, is_active: true,
  }

  function resetRuleForm() {
    ruleForm = { name: '', description: '', summarization_enabled: true, token_threshold: 0, keep_recent: 0, prompt: '', tag: '', execution_order: 10, is_active: true }
    editingRuleId = null
  }

  function startEditRule(r: any) {
    editingRuleId = r.id
    ruleForm = {
      name: r.name, description: r.description || '',
      summarization_enabled: r.summarization_enabled,
      token_threshold: r.token_threshold || 0,
      keep_recent: r.keep_recent || 0,
      prompt: r.prompt || '', tag: r.tag || '',
      execution_order: r.execution_order || 10, is_active: r.is_active,
    }
    showRuleForm = true
  }

  async function load() {
    loading = true
    try {
      const [memData, sumData, ruleData] = await Promise.all([
        api.listMemories(filterConversation || undefined),
        api.listSummaries(),
        api.listMemoryRules(),
      ])
      memories = memData.memories
      summaries = sumData.summaries
      memoryRules = ruleData.rules
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

  async function handleSaveRule() {
    error = ''
    try {
      if (editingRuleId) {
        await api.updateMemoryRule(editingRuleId, ruleForm)
      } else {
        await api.createMemoryRule(ruleForm)
      }
      showRuleForm = false
      resetRuleForm()
      await load()
    } catch (e: any) { error = e.message }
  }

  async function handleDeleteRule(id: string) {
    if (!confirm('Delete this memory rule?')) return
    try { await api.deleteMemoryRule(id); await load() }
    catch (e: any) { error = e.message }
  }

  async function toggleRuleActive(r: any) {
    try { await api.updateMemoryRule(r.id, { is_active: !r.is_active }); await load() }
    catch (e: any) { error = e.message }
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
  <div class="card-header">
    <h3>Memory Rules</h3>
    <button class="primary" onclick={() => { resetRuleForm(); showRuleForm = true; }}>+ Add Rule</button>
  </div>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    Override summarization behavior per conversation. Tagged rules activate via <code style="color: var(--accent);">&lt;#memory-rule-tag#&gt;</code>. A rule with no tag acts as the default. Fields set to 0 or empty inherit global settings.
  </p>
  {#if showRuleForm}
    <div class="card" style="background: var(--bg-elevated); margin-bottom: 16px;">
      <div class="form-group">
        <label for="rule-name">Name</label>
        <input id="rule-name" bind:value={ruleForm.name} placeholder="Rule name" />
      </div>
      <div class="form-row">
        <div class="form-group">
          <label for="rule-tag">Tag (optional)</label>
          <input id="rule-tag" bind:value={ruleForm.tag} placeholder="e.g. slow-burn" />
        </div>
        <div class="form-group">
          <label for="rule-order">Execution Order</label>
          <input id="rule-order" type="number" bind:value={ruleForm.execution_order} />
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label for="rule-threshold">Token Threshold (0 = global)</label>
          <input id="rule-threshold" type="number" bind:value={ruleForm.token_threshold} />
        </div>
        <div class="form-group">
          <label for="rule-recent">Keep Recent (0 = global)</label>
          <input id="rule-recent" type="number" bind:value={ruleForm.keep_recent} />
        </div>
      </div>
      <div class="form-group">
        <label><input type="checkbox" bind:checked={ruleForm.summarization_enabled} style="width: auto;"> Summarization Enabled</label>
      </div>
      <div class="form-group">
        <label for="rule-prompt">Custom Prompt (empty = global)</label>
        <textarea id="rule-prompt" bind:value={ruleForm.prompt} rows="3" style="width: 100%;"></textarea>
      </div>
      <div style="display: flex; gap: 8px;">
        <button class="primary" onclick={handleSaveRule}>{editingRuleId ? 'Update' : 'Create'}</button>
        <button onclick={() => { showRuleForm = false; resetRuleForm(); }}>Cancel</button>
      </div>
    </div>
  {/if}
  {#if memoryRules.length === 0}
    <div class="empty-state" style="padding: 16px;">
      No memory rules configured. Rules let you customize summarization behavior per conversation.
    </div>
  {:else}
    <table>
      <thead>
        <tr><th>Name</th><th>Tag</th><th>Summary</th><th>Threshold</th><th>Active</th><th>Actions</th></tr>
      </thead>
      <tbody>
        {#each memoryRules as r}
          <tr>
            <td>{r.name}</td>
            <td>{#if r.tag}<span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px;">{r.tag}</span>{:else}<span style="color: var(--text-dim); font-size: 11px;">default</span>{/if}</td>
            <td>{r.summarization_enabled ? 'ON' : 'OFF'}</td>
            <td style="font-size: 12px;">{r.token_threshold > 0 ? r.token_threshold : 'global'}</td>
            <td>
              <button onclick={() => toggleRuleActive(r)} style="padding: 2px 8px; font-size: 11px;" class={r.is_active ? 'primary' : ''}>{r.is_active ? 'ON' : 'OFF'}</button>
            </td>
            <td>
              <button onclick={() => startEditRule(r)} style="font-size: 12px;">Edit</button>
              <button class="danger" onclick={() => handleDeleteRule(r.id)} style="font-size: 12px;">Delete</button>
            </td>
          </tr>
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
