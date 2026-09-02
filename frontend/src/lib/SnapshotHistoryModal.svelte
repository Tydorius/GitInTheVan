<script lang="ts">
  /**
   * Version history for one object (Phase 25).
   *
   * One component for every type that keeps snapshots -- the API is generic, so
   * the only per-page work is a History button that sets these props. The diff
   * reuses lib/assistant/diff.ts and the global .diff-block styles the
   * assistant's confirmation card already uses, so a version comparison looks
   * the same wherever the user meets one.
   */
  import { api } from '../api'
  import { lineDiff, diffStats } from './assistant/diff'

  export let show: boolean = false
  export let resourceType: string = 'cantrip'
  export let resourceId: string = ''
  export let resourceName: string = ''
  /** Called after anything that changed the live object, so the page reloads. */
  export let onRestored: (() => void) | null = null

  let snapshots: any[] = []
  let loading = false
  let busy = false
  let errorMsg = ''
  let notes: string[] = []
  let selectedId = ''
  let selected: any = null
  let current: any = null
  let saveLabel = ''

  const FIELD_ORDER = ['code', 'content', 'prompt', 'llm_instructions', 'description']

  $: if (show && resourceId) {
    void open()
  }

  async function open() {
    if (loading) return
    errorMsg = ''
    notes = []
    selectedId = ''
    selected = null
    await load()
  }

  async function load() {
    loading = true
    try {
      const data = await api.listSnapshots(resourceType, resourceId)
      snapshots = data.snapshots
      current = await api.getResourceForSnapshot(resourceType, resourceId)
    } catch (e: any) {
      errorMsg = e.message
    } finally {
      loading = false
    }
  }

  async function select(id: string) {
    if (selectedId === id) {
      selectedId = ''
      selected = null
      return
    }
    errorMsg = ''
    try {
      selected = await api.getSnapshot(id)
      selectedId = id
    } catch (e: any) {
      errorMsg = e.message
    }
  }

  /** Fields worth diffing: long text, and anything that differs from the live object. */
  function changedFields(content: any): string[] {
    if (!content) return []
    const keys = Object.keys(content).filter((k) => k !== 'entries')
    const interesting = keys.filter((k) => {
      const value = content[k]
      if (typeof value === 'string' && value.length > 120) return true
      if (!current) return false
      return JSON.stringify(current[k]) !== JSON.stringify(value)
    })
    return interesting.sort(
      (a, b) => FIELD_ORDER.indexOf(b) - FIELD_ORDER.indexOf(a) || a.localeCompare(b),
    )
  }

  function asText(value: any): string {
    if (value === undefined || value === null) return ''
    return typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  }

  function entryCount(content: any): number | null {
    if (!content || !Array.isArray(content.entries)) return null
    return content.entries.length
  }

  function when(iso: string): string {
    if (!iso) return ''
    const d = new Date(iso)
    return isNaN(d.getTime()) ? iso : d.toLocaleString()
  }

  function sizeLabel(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`
    return `${(bytes / 1024).toFixed(1)} KB`
  }

  async function saveVersion() {
    busy = true
    errorMsg = ''
    notes = []
    try {
      await api.createSnapshot(resourceType, resourceId, saveLabel.trim())
      saveLabel = ''
      await load()
    } catch (e: any) {
      errorMsg = e.message
    } finally {
      busy = false
    }
  }

  async function restoreAsNew(id: string) {
    busy = true
    errorMsg = ''
    notes = []
    try {
      const res = await api.restoreSnapshotAsNew(id, '')
      notes = [`Created "${res.name}" as a new ${res.resource_type.replace(/_/g, ' ')}.`, ...res.notes]
      onRestored?.()
    } catch (e: any) {
      errorMsg = e.message
    } finally {
      busy = false
    }
  }

  async function restoreInPlace(id: string) {
    if (
      !confirm(
        `Replace "${resourceName}" with this stored version?\n\n` +
          'The version you have now is saved first, so you can undo this.',
      )
    )
      return
    busy = true
    errorMsg = ''
    notes = []
    try {
      const res = await api.restoreSnapshotInPlace(id)
      notes = ['Restored. The version this replaced is now the newest entry below.', ...res.notes]
      selectedId = ''
      selected = null
      await load()
      onRestored?.()
    } catch (e: any) {
      errorMsg = e.message
    } finally {
      busy = false
    }
  }

  async function remove(id: string) {
    if (!confirm('Delete this stored version? This cannot be undone.')) return
    busy = true
    errorMsg = ''
    try {
      await api.deleteSnapshot(id)
      if (selectedId === id) {
        selectedId = ''
        selected = null
      }
      await load()
    } catch (e: any) {
      errorMsg = e.message
    } finally {
      busy = false
    }
  }

  function close() {
    show = false
    errorMsg = ''
    notes = []
    selectedId = ''
    selected = null
  }
</script>

{#if show}
  <div
    class="modal-overlay"
    role="dialog"
    tabindex="-1"
    onclick={(e) => {
      if (e.target === e.currentTarget) close()
    }}
  >
    <div class="modal" style="width: 720px;">
      <h3>History &mdash; {resourceName}</h3>
      <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">
        A version is stored automatically before every change and before a delete. Restoring over
        this object saves what it replaces first, so a restore can itself be undone.
      </p>

      {#if errorMsg}<div class="error-msg" style="margin-bottom: 12px;">{errorMsg}</div>{/if}
      {#if notes.length}
        <div class="success-msg" style="margin-bottom: 12px;">
          {#each notes as note}<div>{note}</div>{/each}
        </div>
      {/if}

      <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 12px;">
        <input
          bind:value={saveLabel}
          placeholder="Name this version (optional)"
          style="flex: 1;"
          autocomplete="off"
        />
        <button onclick={saveVersion} disabled={busy || loading}>Save a version</button>
      </div>

      {#if loading}
        <p style="color: var(--text-dim);">Loading&hellip;</p>
      {:else if snapshots.length === 0}
        <p style="color: var(--text-dim);">
          No stored versions yet. One is kept automatically the first time this object changes.
        </p>
      {:else}
        <div style="max-height: 380px; overflow-y: auto;">
          {#each snapshots as snap (snap.id)}
            <div class="card" style="padding: 10px; margin-bottom: 8px;">
              <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                <span style="font-size: 12px;">{when(snap.created_at)}</span>
                <span class="badge" title={snap.source === 'manual' ? 'Saved by you; never pruned' : 'Stored automatically before a change'}>
                  {snap.source === 'manual' ? 'Saved' : 'Automatic'}
                </span>
                {#if snap.label}<span style="font-size: 12px;">{snap.label}</span>{/if}
                <span style="font-size: 11px; color: var(--text-dim);">{sizeLabel(snap.size_bytes)}</span>
                <span style="flex: 1;"></span>
                <button onclick={() => select(snap.id)}>
                  {selectedId === snap.id ? 'Hide' : 'Compare'}
                </button>
                <button onclick={() => restoreAsNew(snap.id)} disabled={busy}>Restore as a copy</button>
                <button onclick={() => restoreInPlace(snap.id)} disabled={busy}>Restore over this</button>
                <button class="danger" onclick={() => remove(snap.id)} disabled={busy}>Delete</button>
              </div>

              {#if selectedId === snap.id && selected}
                {@const fields = changedFields(selected.content)}
                {@const entries = entryCount(selected.content)}
                <div style="margin-top: 10px;">
                  {#if entries !== null}
                    <div class="diff-stats">
                      This version has {entries} entr{entries === 1 ? 'y' : 'ies'}.
                    </div>
                  {/if}
                  {#if fields.length === 0}
                    <div class="diff-stats">Identical to what you have now.</div>
                  {/if}
                  {#each fields as field}
                    {@const diff = lineDiff(asText(current?.[field]), asText(selected.content[field]))}
                    {@const stats = diffStats(diff)}
                    <div class="diff-stats">{field}: +{stats.added} / -{stats.removed}</div>
                    <pre class="diff-block">{#each diff as line, i (i)}<div class="diff-line diff-{line.kind}">{line.kind === 'add' ? '+ ' : line.kind === 'del' ? '- ' : '  '}{line.text}</div>{/each}</pre>
                  {/each}
                </div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}

      <div class="modal-actions">
        <button onclick={close}>Close</button>
      </div>
    </div>
  </div>
{/if}
