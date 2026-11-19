<script lang="ts">
  import { api } from '../api'
  import { downloadJson } from '../lib/download'
  import { onMount } from 'svelte'
  import CollapsibleCard from '../lib/CollapsibleCard.svelte'
  import { CollapseController } from '../lib/collapse'
  import StorageRows from '../lib/StorageRows.svelte'

  let collapse = new CollapseController('storage', ['chat-data', 'cantrip-data', 'user-data'])

  let error = ''
  let conversations: any[] = []
  let convSearch = ''
  let selectedConversation = ''
  let chatReload = 0

  let cantrips: any[] = []
  let selectedCantrip = ''
  let cantripReload = 0
  let userReload = 0

  let importTargetConversation = ''

  async function loadConversations() {
    try {
      const data = await api.listStorageChats(convSearch)
      conversations = data.conversations
      if (selectedConversation && !conversations.some((c: any) => c.conversation_id === selectedConversation)) {
        selectedConversation = ''
      }
    } catch (e: any) { error = e.message }
  }

  async function exportConversation(conversationId: string) {
    error = ''
    try {
      const data = await api.exportStorage('chat', conversationId)
      downloadJson(data, `gitv-storage-chat-${conversationId.slice(0, 24)}.json`)
    } catch (e: any) { error = e.message }
  }

  async function copyConversation(conversationId: string) {
    const target = prompt('Copy all rows to conversation id:', '')
    if (!target) return
    error = ''
    try {
      const res = await api.copyStorageConversation(conversationId, target)
      alert(`Copied ${res.copied} row(s)${res.skipped.length ? `, skipped existing keys: ${res.skipped.join(', ')}` : ''}`)
      await loadConversations()
    } catch (e: any) { error = e.message }
  }

  async function clearConversation(conversationId: string) {
    if (!confirm(`Delete ALL stored chat-data rows for conversation "${conversationId}"? This cannot be undone.`)) return
    error = ''
    try {
      await api.clearStorageConversation(conversationId)
      await loadConversations()
      chatReload++
    } catch (e: any) { error = e.message }
  }

  async function exportCantripData(cantripId: string) {
    error = ''
    try {
      const data = await api.exportStorage('cantrip', '', cantripId)
      const name = cantrips.find((c: any) => c.id === cantripId)?.name || 'cantrip'
      downloadJson(data, `gitv-storage-cantrip-${name}.json`)
    } catch (e: any) { error = e.message }
  }

  async function exportUserData() {
    error = ''
    try {
      const data = await api.exportStorage('user')
      downloadJson(data, 'gitv-storage-user-data.json')
    } catch (e: any) { error = e.message }
  }

  function pickImportFile(target: 'conversation' | 'cantrip' | 'user', event: Event) {
    const input = event.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = async () => {
      error = ''
      try {
        const payload = JSON.parse(String(reader.result))
        const res = await api.importStorage(
          payload,
          target === 'conversation' ? importTargetConversation.trim() : '',
          target === 'cantrip' ? selectedCantrip : '',
        )
        let note = `Imported ${res.imported} row(s).`
        if (res.skipped.length) note += ` Skipped: ${res.skipped.join('; ')}`
        alert(note)
        if (target === 'conversation') { await loadConversations(); chatReload++ }
        else if (target === 'cantrip') { cantripReload++ }
        else { userReload++ }
      } catch (e: any) { error = e.message }
      finally { input.value = '' }
    }
    reader.readAsText(file)
  }

  onMount(() => {
    loadConversations()
    api.listCantrips().then((data: any) => { cantrips = data.cantrips }).catch((e: any) => { error = e.message })
  })
</script>

<div class="page-header">
  <h1>Storage</h1>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

<CollapsibleCard title="Chat Data (per-conversation)" cardKey="chat-data" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">
    Persistent <code>context.chat_data</code> rows, grouped by conversation. Nothing here is editable by the LLM — these are the values your cantrips wrote.
  </p>
  <div class="form-row" style="align-items: center; margin-bottom: 8px;">
    <input
      placeholder="Search conversations..."
      bind:value={convSearch}
      onkeydown={(e) => { if (e.key === 'Enter') loadConversations(); }}
      style="flex: 1;"
    />
    <button onclick={loadConversations}>Search</button>
  </div>
  {#if conversations.length}
    <div style="display: grid; gap: 4px; margin-bottom: 12px;">
      {#each conversations as c (c.conversation_id)}
        <div
          style="border: 1px solid {selectedConversation === c.conversation_id ? 'var(--accent, #4a9)' : 'var(--border)'}; border-radius: 4px; padding: 6px 8px; display: flex; justify-content: space-between; align-items: center; gap: 8px; cursor: pointer;"
          onclick={() => { selectedConversation = c.conversation_id; chatReload++; }}
        >
          <div style="min-width: 0;">
            <div style="font-family: monospace; font-size: 12px; word-break: break-all;">{c.conversation_id}</div>
            <div style="color: var(--text-dim); font-size: 11px;">{c.key_count} key(s) · {c.updated_at ? new Date(c.updated_at).toLocaleString() : ''}</div>
          </div>
          <div style="display: flex; gap: 4px; flex-shrink: 0;" onclick={(e) => e.stopPropagation()}>
            <button style="font-size: 11px; padding: 2px 8px;" onclick={() => exportConversation(c.conversation_id)}>Export</button>
            <button style="font-size: 11px; padding: 2px 8px;" onclick={() => copyConversation(c.conversation_id)}>Copy To…</button>
            <button class="danger" style="font-size: 11px; padding: 2px 8px;" onclick={() => clearConversation(c.conversation_id)}>Clear</button>
          </div>
        </div>
      {/each}
    </div>
  {:else}
    <div style="color: var(--text-dim); font-size: 13px; padding: 8px 0;">No conversations with stored chat data.</div>
  {/if}

  {#if selectedConversation}
    <StorageRows scope="chat-data" contextId={selectedConversation} reloadKey={chatReload} />
  {/if}

  <div class="form-row" style="align-items: center; margin-top: 12px;">
    <input placeholder="Import target conversation id (for chat bundles without one)" bind:value={importTargetConversation} style="flex: 1;" />
    <label class="button-label" style="font-size: 12px;">
      <input type="file" accept="application/json" style="display: none;" onchange={(e) => pickImportFile('conversation', e)} />
      <span class="btn-lookalike">Import…</span>
    </label>
  </div>
</CollapsibleCard>

<CollapsibleCard title="Cantrip Data (per-cantrip)" cardKey="cantrip-data" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">
    Persistent <code>context.cantrip_data</code> rows — the per-cantrip store that survives across chats.
  </p>
  <div class="form-group">
    <label for="storage-cantrip">Cantrip</label>
    <select id="storage-cantrip" bind:value={selectedCantrip}>
      <option value="">Select a cantrip...</option>
      {#each cantrips as c}<option value={c.id}>{c.name}</option>{/each}
    </select>
  </div>
  {#if selectedCantrip}
    <StorageRows scope="cantrip-data" contextId={selectedCantrip} reloadKey={cantripReload} />
    <div style="margin-top: 12px;">
      <button onclick={() => exportCantripData(selectedCantrip)}>Export This Cantrip's Data</button>
    </div>
  {/if}
  <div class="form-row" style="align-items: center; margin-top: 12px;">
    <span style="color: var(--text-dim); font-size: 12px;">Cantrip-data bundles import into the cantrip selected above.</span>
    <label class="button-label" style="font-size: 12px;">
      <input type="file" accept="application/json" style="display: none;" onchange={(e) => pickImportFile('cantrip', e)} />
      <span class="btn-lookalike">Import…</span>
    </label>
  </div>
</CollapsibleCard>

<CollapsibleCard title="User Data (global)" cardKey="user-data" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">
    Persistent <code>context.user_data</code> rows — shared across all chats and cantrips.
  </p>
  <StorageRows scope="user-data" reloadKey={userReload} />
  <div class="form-row" style="align-items: center; margin-top: 12px;">
    <button onclick={exportUserData}>Export All</button>
    <label class="button-label" style="font-size: 12px;">
      <input type="file" accept="application/json" style="display: none;" onchange={(e) => pickImportFile('user', e)} />
      <span class="btn-lookalike">Import…</span>
    </label>
  </div>
</CollapsibleCard>

<style>
  .button-label .btn-lookalike {
    display: inline-block;
    padding: 4px 12px;
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
    background: var(--bg2, var(--bg, #222));
  }
  .button-label:hover .btn-lookalike {
    border-color: var(--accent, #4a9);
  }
</style>
