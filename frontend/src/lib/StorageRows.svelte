<script lang="ts">
  import { api } from '../api'

  export let scope: 'chat-data' | 'user-data' | 'cantrip-data'
  export let contextId: string | null = null
  export let reloadKey: number = 0

  let rows: any[] = []
  let total = 0
  let search = ''
  let offset = 0
  const limit = 100
  let loading = false
  let error = ''
  let editingId: string | null = null
  let editKey = ''
  let editValue = ''
  let expandedId: string | null = null
  let expandedValue = ''

  async function refresh() {
    if (scope !== 'user-data' && !contextId) {
      rows = []
      total = 0
      return
    }
    loading = true
    error = ''
    try {
      const data = await api.listStorageRows(scope, {
        ...(scope === 'chat-data' ? { conversation_id: contextId! } : {}),
        ...(scope === 'cantrip-data' ? { cantrip_id: contextId! } : {}),
        search, limit, offset,
      })
      rows = data.rows
      total = data.total
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  $: scope, contextId, search, offset, reloadKey, refresh()

  async function toggleExpand(row: any) {
    if (expandedId === row.id) {
      expandedId = null
      expandedValue = ''
      return
    }
    expandedId = row.id
    expandedValue = ''
    try {
      const full = await api.getStorageRow(scope, row.id)
      expandedValue = JSON.stringify(full.value, null, 2)
    } catch (e: any) { error = e.message }
  }

  function startEdit(row: any) {
    editingId = row.id
    editKey = row.key
    error = ''
    api.getStorageRow(scope, row.id).then((full: any) => {
      editValue = JSON.stringify(full.value, null, 2)
    }).catch((e: any) => { error = e.message })
  }

  function cancelEdit() {
    editingId = null
    editKey = ''
    editValue = ''
  }

  async function saveEdit(id: string) {
    error = ''
    try {
      JSON.parse(editValue)
    } catch (e: any) {
      error = `Value must be valid JSON: ${e.message}`
      return
    }
    try {
      await api.putStorageRow(scope, id, { key: editKey, value: editValue })
      cancelEdit()
      await refresh()
    } catch (e: any) { error = e.message }
  }

  async function remove(id: string) {
    if (!confirm('Delete this row?')) return
    error = ''
    try {
      await api.deleteStorageRow(scope, id)
      await refresh()
    } catch (e: any) { error = e.message }
  }

  async function copyRow(row: any) {
    const target = prompt('Copy to conversation id:', '')
    if (!target) return
    error = ''
    try {
      await api.copyStorageRow(row.id, target)
    } catch (e: any) { error = e.message }
  }
</script>

{#if error}<div class="error-msg">{error}</div>{/if}

<div class="form-row" style="align-items: center; margin-bottom: 8px;">
  <input
    placeholder="Search keys..."
    bind:value={search}
    onkeydown={(e) => { if (e.key === 'Enter') { offset = 0; refresh(); } }}
    style="flex: 1;"
  />
  <span style="color: var(--text-dim); font-size: 12px; white-space: nowrap;">
    {total} row{total === 1 ? '' : 's'}
  </span>
</div>

{#if loading}
  <div class="loading">Loading...</div>
{:else if rows.length === 0}
  <div style="color: var(--text-dim); font-size: 13px; padding: 8px 0;">No rows.</div>
{:else}
  <div style="display: grid; gap: 4px;">
    {#each rows as row (row.id)}
      <div style="border: 1px solid var(--border); border-radius: 4px; padding: 8px;">
        {#if editingId === row.id}
          <div class="form-group">
            <label>Key</label>
            <input bind:value={editKey} />
          </div>
          <div class="form-group">
            <label>Value (JSON)</label>
            <textarea bind:value={editValue} style="min-height: 120px; font-family: monospace;"></textarea>
          </div>
          <div style="display: flex; gap: 8px;">
            <button class="primary" onclick={() => saveEdit(row.id)}>Save</button>
            <button onclick={cancelEdit}>Cancel</button>
          </div>
        {:else}
          <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;">
            <div style="min-width: 0; flex: 1; cursor: pointer;" onclick={() => toggleExpand(row)} title="Click to expand the full value">
              <strong style="font-family: monospace; word-break: break-all;">{row.key}</strong>
              <div style="color: var(--text-dim); font-size: 11px;">
                {row.size} bytes · {row.updated_at ? new Date(row.updated_at).toLocaleString() : ''}
              </div>
              {#if expandedId === row.id}
                <div class="debug-output" style="margin-top: 6px;">{expandedValue || 'Loading…'}</div>
              {:else}
                <div style="color: var(--text-dim); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{row.preview}</div>
              {/if}
            </div>
            <div style="display: flex; gap: 4px; flex-shrink: 0;">
              <button onclick={() => startEdit(row)} style="font-size: 11px; padding: 2px 8px;">Edit</button>
              {#if scope === 'chat-data'}
                <button onclick={() => copyRow(row)} style="font-size: 11px; padding: 2px 8px;">Copy</button>
              {/if}
              <button class="danger" onclick={() => remove(row.id)} style="font-size: 11px; padding: 2px 8px;">Delete</button>
            </div>
          </div>
        {/if}
      </div>
    {/each}
  </div>

  {#if total > limit}
    <div style="display: flex; gap: 8px; margin-top: 8px; align-items: center;">
      <button disabled={offset === 0} onclick={() => { offset = Math.max(0, offset - limit); }}>Previous</button>
      <span style="color: var(--text-dim); font-size: 12px;">
        {offset + 1}–{Math.min(offset + limit, total)} of {total}
      </span>
      <button disabled={offset + limit >= total} onclick={() => { offset = offset + limit; }}>Next</button>
    </div>
  {/if}
{/if}
