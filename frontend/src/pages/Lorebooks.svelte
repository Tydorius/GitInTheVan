<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'
  import CodeEditor from '../lib/CodeEditor.svelte'
  import TagEditModal from '../lib/TagEditModal.svelte'

  let lorebooks: any[] = []
  let loading = true
  let error = ''
  let showForm = false
  let form = {
    name: '', description: '', is_public: false,
    run_pre_driver: true, run_driver_callable: false,
    run_pre_navigator: false, run_post_navigator: false,
  }

  let selectedLorebook: any = null
  let showEntryForm = false
  let editingEntryId: string | null = null
  let jsonMode = false
  let jsonEditData = ''
  let jsonError = ''
  let importJsonError = ''
  let importJsonValid: boolean | null = null

  function validateJson(text: string): { valid: boolean; error: string | null } {
    if (!text.trim()) return { valid: false, error: 'Empty' }
    try {
      JSON.parse(text)
      return { valid: true, error: null }
    } catch (e: any) {
      const msg = e.message || 'Invalid JSON'
      const posMatch = msg.match(/position (\d+)/i)
      if (posMatch) {
        const pos = parseInt(posMatch[1])
        const before = text.substring(0, pos)
        const line = before.split('\n').length
        return { valid: false, error: `Line ${line}: ${msg}` }
      }
      return { valid: false, error: msg }
    }
  }

  function checkImportJson() {
    if (!importJson.trim()) {
      importJsonValid = null
      importJsonError = ''
      return
    }
    const result = validateJson(importJson)
    importJsonValid = result.valid
    importJsonError = result.valid ? '' : result.error || ''
  }
  let entryForm = {
    name: '', keys: '', secondary_keys: '', content: '',
    position: 'before_last_message', insertion_order: 10,
    is_constant: false, is_selective: false, is_disabled: false,
  }

  let showImport = false
  let importJson = ''
  let importName = ''

  async function load() {
    loading = true
    try { const data = await api.listLorebooks(); lorebooks = data.lorebooks }
    catch (e: any) { error = e.message }
    finally { loading = false }
  }

  async function handleSubmit() {
    error = ''
    try {
      await api.createLorebook(form)
      showForm = false
      form = { name: '', description: '', is_public: false, run_pre_driver: true, run_driver_callable: false, run_pre_navigator: false, run_post_navigator: false }
      await load()
    }
    catch (e: any) { error = e.message }
  }

  let positionLb: any = null

  async function savePositions() {
    if (!positionLb) return
    error = ''
    try {
      await api.updateLorebook(positionLb.id, {
        run_pre_driver: positionLb.run_pre_driver,
        run_driver_callable: positionLb.run_driver_callable,
        run_pre_navigator: positionLb.run_pre_navigator,
        run_post_navigator: positionLb.run_post_navigator,
      })
      positionLb = null
      await load()
    } catch (e: any) { error = e.message }
  }

  async function handleDelete(id: string) {
    if (!confirm('Delete this lorebook and all its entries?')) return
    try {
      if (selectedLorebook?.id === id) selectedLorebook = null
      await api.deleteLorebook(id); await load()
    }
    catch (e: any) { error = e.message }
  }

  let tagModal = { show: false, id: '', name: '', tag: '' }
  let tagError = ''

  function openTagModal(lb: any) {
    tagError = ''
    tagModal = { show: true, id: lb.id, name: lb.name, tag: lb.tag || '' }
  }

  async function saveTag(tag: string) {
    try { await api.updateLorebook(tagModal.id, { tag }); tagModal.show = false; await load() }
    catch (e: any) { error = e.message }
  }

  async function toggleActive(lb: any) {
    try {
      await api.updateLorebook(lb.id, { is_active: !lb.is_active })
      await load()
    } catch (e: any) { error = e.message }
  }

  async function openLorebook(lb: any) {
    try {
      selectedLorebook = await api.getLorebook(lb.id)
      jsonMode = false
      jsonError = ''
    } catch (e: any) { error = e.message }
  }

  function enterJsonMode() {
    jsonError = ''
    const entries = (selectedLorebook.entries || []).map((e: any) => ({
      name: e.name || '',
      keys: e.keys || [],
      secondary_keys: e.secondary_keys || [],
      content: e.content || '',
      position: e.position || 'before_last_message',
      insertion_order: e.insertion_order || 10,
      is_constant: e.is_constant || false,
      is_selective: e.is_selective || false,
      is_disabled: e.is_disabled || false,
    }))
    jsonEditData = JSON.stringify({
      name: selectedLorebook.name,
      description: selectedLorebook.description || '',
      entries,
    }, null, 2)
    jsonMode = true
  }

  async function saveJsonMode() {
    jsonError = ''
    try {
      const data = JSON.parse(jsonEditData)
      let entries: any[]
      if (Array.isArray(data)) {
        entries = data
      } else if (data.entries && Array.isArray(data.entries)) {
        entries = data.entries
      } else if (data.entries && typeof data.entries === 'object') {
        entries = Object.values(data.entries)
      } else {
        entries = []
      }
      for (const e of entries) {
        await api.addLorebookEntry(selectedLorebook.id, {
          name: e.name || '',
          keys: Array.isArray(e.keys) ? e.keys : [],
          secondary_keys: Array.isArray(e.secondary_keys) ? e.secondary_keys : [],
          content: e.content || '',
          position: e.position || 'before_last_message',
          insertion_order: e.insertion_order || 10,
          is_constant: e.is_constant || false,
          is_selective: e.is_selective || false,
          is_disabled: e.is_disabled || false,
        })
      }
      jsonMode = false
      await openLorebook(selectedLorebook)
    } catch (e: any) {
      jsonError = e.message || 'Invalid JSON'
    }
  }

  function resetEntryForm() {
    entryForm = { name: '', keys: '', secondary_keys: '', content: '', position: 'before_last_message', insertion_order: 10, is_constant: false, is_selective: false, is_disabled: false }
    editingEntryId = null
  }

  function startEditEntry(e: any) {
    editingEntryId = e.id
    entryForm = {
      name: e.name || '',
      keys: Array.isArray(e.keys) ? e.keys.join(', ') : '',
      secondary_keys: Array.isArray(e.secondary_keys) ? e.secondary_keys.join(', ') : '',
      content: e.content || '',
      position: e.position || 'before_last_message',
      insertion_order: e.insertion_order || 10,
      is_constant: e.is_constant || false,
      is_selective: e.is_selective || false,
      is_disabled: e.is_disabled || false,
    }
    showEntryForm = true
  }

  async function handleEntrySubmit() {
    error = ''
    try {
      const data = {
        name: entryForm.name,
        keys: entryForm.keys.split(',').map((k: string) => k.trim()).filter(Boolean),
        secondary_keys: entryForm.secondary_keys.split(',').map((k: string) => k.trim()).filter(Boolean),
        content: entryForm.content,
        position: entryForm.position,
        insertion_order: entryForm.insertion_order,
        is_constant: entryForm.is_constant,
        is_selective: entryForm.is_selective,
        is_disabled: entryForm.is_disabled,
      }
      if (editingEntryId) {
        await api.updateLorebookEntry(selectedLorebook.id, editingEntryId, data)
      } else {
        await api.addLorebookEntry(selectedLorebook.id, data)
      }
      showEntryForm = false
      resetEntryForm()
      await openLorebook(selectedLorebook)
    } catch (e: any) { error = e.message }
  }

  async function handleDeleteEntry(entryId: string) {
    if (!confirm('Delete this entry?')) return
    try { await api.deleteLorebookEntry(selectedLorebook.id, entryId); await openLorebook(selectedLorebook) }
    catch (e: any) { error = e.message }
  }

  async function handleImport() {
    error = ''
    try {
      const data = JSON.parse(importJson)
      let entries: any[]
      if (Array.isArray(data)) {
        entries = data
      } else if (data.entries && Array.isArray(data.entries)) {
        entries = data.entries
      } else if (data.entries && typeof data.entries === 'object') {
        entries = Object.values(data.entries)
      } else {
        entries = []
      }
      const payload = {
        name: importName || (!Array.isArray(data) ? data.name : '') || 'Imported Lorebook',
        description: Array.isArray(data) ? '' : (data.description || ''),
        entries,
      }
      await api.importLorebook(payload)
      showImport = false
      importJson = ''
      importName = ''
      importJsonValid = null
      importJsonError = ''
      await load()
    } catch (e: any) {
      error = e.message || 'Invalid JSON'
    }
  }

  function handleFileLoad(e: Event) {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      importJson = reader.result as string
      try {
        const data = JSON.parse(importJson)
        if (data.name && !importName) importName = data.name
      } catch {}
    }
    reader.readAsText(file)
  }

  async function handleExport(id: string) {
    try {
      const data = await api.exportLorebook(id)
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${data.name || 'lorebook'}.json`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e: any) { error = e.message }
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Lorebooks <a class="help-link" href="/help/user-guide.html#lorebooks" target="_blank" title="Open documentation">?</a></h2>
  <div>
    <button onclick={() => showImport = true}>Import JSON</button>
    <button class="primary" onclick={() => showForm = true}>+ Add Lorebook</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

{#if loading}<div class="loading">Loading...</div>
{:else if lorebooks.length === 0 && !showForm}
  <div class="empty-state">No lorebooks configured. Click "Add Lorebook" or "Import JSON" to get started.</div>
{:else}
  {#if !selectedLorebook}
    <table>
      <thead><tr><th>Name</th><th>Entries</th><th>Active</th><th>Visibility</th><th>Actions</th></tr></thead>
      <tbody>
        {#each lorebooks as lb}
          <tr>
            <td>
              <div><a href="#" onclick={(e) => { e.preventDefault(); openLorebook(lb); }}>{lb.name}</a></div>
              {#if lb.tag}
                <span style="display: inline-flex; align-items: center; gap: 4px; margin-top: 2px;">
                  <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; cursor: pointer;"
                        onclick={() => navigator.clipboard.writeText('<#lore-' + lb.tag + '#>')}
                        title="Click to copy">&lt;#lore-{lb.tag}#&gt;</span>
                  <button onclick={() => openTagModal(lb)} style="padding: 0 4px; font-size: 14px; border: none; background: none; cursor: pointer;" title="Edit tag">🖉</button>
                </span>
              {:else}
                <button onclick={() => openTagModal(lb)} style="margin-top: 2px; padding: 2px 8px; font-size: 11px;">+ Tag</button>
              {/if}
            </td>
            <td>{lb.entry_count}</td>
            <td>
              <button
                onclick={() => toggleActive(lb)}
                style="padding: 2px 8px; font-size: 11px;"
                class={lb.is_active ? 'primary' : ''}
              >{lb.is_active ? 'ON' : 'OFF'}</button>
            </td>
            <td>{#if lb.is_public}<span class="badge active">Public</span>{:else}<span class="badge inactive">Private</span>{/if}</td>
            <td>
              <button onclick={() => openLorebook(lb)}>Manage</button>
              <button onclick={() => handleExport(lb.id)}>Export</button>
              <button class="danger" onclick={() => handleDelete(lb.id)}>Delete</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <div class="card">
      <div class="card-header">
        <div>
          <strong>{selectedLorebook.name}</strong>
          {#if selectedLorebook.is_public}<span class="badge active" style="margin-left: 8px;">Public</span>{/if}
        </div>
        <div>
          {#if jsonMode}
            <button class="primary" onclick={saveJsonMode}>Save JSON</button>
            <button onclick={() => jsonMode = false}>Cancel</button>
          {:else}
            <button onclick={enterJsonMode}>Manual JSON</button>
            <button class="primary" onclick={() => { resetEntryForm(); showEntryForm = true; }}>+ Add Entry</button>
            <button onclick={() => selectedLorebook = null}>Back</button>
          {/if}
        </div>
      </div>
      {#if selectedLorebook.description}<p style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">{selectedLorebook.description}</p>{/if}
      {#if selectedLorebook.tag}
        <div style="margin-bottom: 16px;">
          <span style="font-size: 11px; color: var(--text-dim);">Tag: </span>
          <span style="display: inline-flex; align-items: center; gap: 4px;">
            <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; cursor: pointer;"
                  onclick={() => navigator.clipboard.writeText('<#lore-' + selectedLorebook.tag + '#>')}
                  title="Click to copy">&lt;#lore-{selectedLorebook.tag}#&gt;</span>
            <button onclick={() => openTagModal(selectedLorebook)} style="padding: 0 4px; font-size: 14px; border: none; background: none; cursor: pointer;" title="Edit tag">🖉</button>
          </span>
        </div>
      {/if}

      {#if jsonMode}
        <div class="form-group" style="margin-top: 12px;">
          <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 8px;">Edit the raw lorebook JSON. Saving will replace all entries.</p>
          {#if jsonError}<div class="error-msg">{jsonError}</div>{/if}
          <CodeEditor bind:value={jsonEditData} language="json" minHeight="400px" />
        </div>
      {:else if selectedLorebook.entries?.length === 0}
        <div class="empty-state">No entries yet. Click "Add Entry" to create one.</div>
      {:else}
        <table>
          <thead><tr><th>Name</th><th>Keywords</th><th>Position</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody>
            {#each selectedLorebook.entries as e}
              <tr>
                <td>{e.name || '(unnamed)'}</td>
                <td style="font-size: 11px; color: var(--text-dim);">{Array.isArray(e.keys) ? e.keys.join(', ') : ''}</td>
                <td style="font-size: 11px;">{e.position}</td>
                <td>
                  {#if e.is_disabled}<span class="badge inactive">Disabled</span>
                  {:else if e.is_constant}<span class="badge active">Constant</span>
                  {:else}<span class="badge active">Active</span>{/if}
                </td>
                <td>
                  <button onclick={() => startEditEntry(e)}>Edit</button>
                  <button class="danger" onclick={() => handleDeleteEntry(e.id)}>Delete</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}

      <div class="form-group" style="margin-top: 16px;">
        <label>Pipeline Positions</label>
        <div style="display: flex; flex-wrap: wrap; gap: 12px; margin-top: 4px; align-items: center;">
          <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
            <input type="checkbox" checked={selectedLorebook.run_pre_driver} disabled style="width: auto;"> Pre-Driver
          </label>
          <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
            <input type="checkbox" checked={selectedLorebook.run_driver_callable} disabled style="width: auto;"> Driver-Callable
          </label>
          <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
            <input type="checkbox" checked={selectedLorebook.run_pre_navigator} disabled style="width: auto;"> Pre-Navigator
          </label>
          <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
            <input type="checkbox" checked={selectedLorebook.run_post_navigator} disabled style="width: auto;"> Post-Navigator
          </label>
          <button onclick={() => { positionLb = { ...selectedLorebook } }} style="font-size: 11px; padding: 2px 8px;">Edit Positions</button>
        </div>
      </div>
    </div>
  {/if}
{/if}

<!-- Add Lorebook Modal -->
{#if showForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showForm = false; }}>
    <div class="modal">
      <h3>Add Lorebook</h3>
      <form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
        <div class="form-group"><label for="lb-name">Name</label><input id="lb-name" autocomplete="off" bind:value={form.name} required /></div>
        <div class="form-group"><label for="lb-desc">Description</label><input id="lb-desc" autocomplete="off" bind:value={form.description} /></div>
        <div class="form-group"><label><input type="checkbox" bind:checked={form.is_public} style="width: auto;"> Public</label></div>
        <div class="modal-actions">
          <button onclick={() => showForm = false}>Cancel</button>
          <button type="submit" class="primary">Create</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Add/Edit Entry Modal -->
{#if showEntryForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showEntryForm = false; }}>
    <div class="modal" style="width: 700px;">
      <h3>{editingEntryId ? 'Edit Entry' : 'Add Entry'}</h3>
      <form onsubmit={(e) => { e.preventDefault(); handleEntrySubmit(); }}>
        <div class="form-group"><label for="entry-name">Entry Name</label><input id="entry-name" autocomplete="off" bind:value={entryForm.name} placeholder="Castle Lore" /></div>
        <div class="form-row">
          <div class="form-group"><label for="entry-keys">Keywords (comma-separated)</label><input id="entry-keys" autocomplete="off" bind:value={entryForm.keys} placeholder="castle, throne, keep" /></div>
          <div class="form-group"><label for="entry-skeys">Secondary Keys (comma-separated)</label><input id="entry-skeys" autocomplete="off" bind:value={entryForm.secondary_keys} placeholder="king, queen" /></div>
        </div>
        <div class="form-group"><label for="entry-content">Content</label>
          <CodeEditor bind:value={entryForm.content} language="javascript" minHeight="120px" placeholder="The castle has seven towers..." />
        </div>
        <div class="form-row">
          <div class="form-group"><label for="entry-pos">Position</label>
            <select id="entry-pos" bind:value={entryForm.position}>
              <option value="before_last_message">Before Last Message</option>
              <option value="system_start">System Start</option>
            </select>
          </div>
          <div class="form-group"><label for="entry-order">Insertion Order</label><input id="entry-order" type="number" bind:value={entryForm.insertion_order} /></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label><input type="checkbox" bind:checked={entryForm.is_constant} style="width: auto;"> Always Include (Constant)</label></div>
          <div class="form-group"><label><input type="checkbox" bind:checked={entryForm.is_selective} style="width: auto;"> Selective (requires secondary key)</label></div>
        </div>
        <div class="form-group"><label><input type="checkbox" bind:checked={entryForm.is_disabled} style="width: auto;"> Disabled</label></div>
        <div class="modal-actions">
          <button onclick={() => { showEntryForm = false; resetEntryForm(); }}>Cancel</button>
          <button type="submit" class="primary">{editingEntryId ? 'Update' : 'Create'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Import JSON Modal -->
{#if showImport}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showImport = false; }}>
    <div class="modal" style="width: 700px;">
      <h3>Import Lorebook JSON</h3>
      <div class="form-group">
        <label for="imp-file">Load from File</label>
        <input id="imp-file" type="file" accept=".json,application/json" style="padding: 4px;" onchange={handleFileLoad} />
      </div>
      <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">Or paste lorebook JSON below (name, description, entries array).</p>
      <div class="form-group"><label for="imp-name">Lorebook Name (optional, uses JSON name if blank)</label><input id="imp-name" autocomplete="off" bind:value={importName} placeholder="My Imported Lorebook" /></div>
      <div class="form-group"><label for="imp-json">JSON Data</label>
        <CodeEditor bind:value={importJson} language="json" minHeight="250px" placeholder="Paste lorebook JSON here" />
        <div style="margin-top: 8px; display: flex; gap: 8px; align-items: center;">
          <button type="button" onclick={checkImportJson} disabled={!importJson.trim()} style="font-size: 12px; padding: 4px 12px;">Validate JSON</button>
          {#if importJsonValid === true}
            <span style="color: var(--success); font-size: 12px;">✓ Valid JSON</span>
          {:else if importJsonValid === false}
            <span style="color: var(--danger); font-size: 12px;">✗ {importJsonError}</span>
          {/if}
        </div>
      </div>
      <div class="modal-actions">
        <button onclick={() => showImport = false}>Cancel</button>
        <button onclick={handleImport} class="primary" disabled={importJsonValid === false}>Import</button>
      </div>
    </div>
  </div>
{/if}

<TagEditModal
  bind:show={tagModal.show}
  resourceType="lore"
  resourceName={tagModal.name}
  currentTag={tagModal.tag}
  bind:errorMsg={tagError}
  onSave={saveTag}
/>

{#if positionLb}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) positionLb = null; }}>
    <div class="modal">
      <h3>Edit Pipeline Positions</h3>
      <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">{positionLb.name}</p>
      <div class="form-group">
        <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
          <input type="checkbox" bind:checked={positionLb.run_pre_driver} style="width: auto;"> Pre-Driver
        </label>
        <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
          <input type="checkbox" bind:checked={positionLb.run_driver_callable} style="width: auto;"> Driver-Callable
        </label>
        <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
          <input type="checkbox" bind:checked={positionLb.run_pre_navigator} style="width: auto;"> Pre-Navigator
        </label>
        <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
          <input type="checkbox" bind:checked={positionLb.run_post_navigator} style="width: auto;"> Post-Navigator
        </label>
      </div>
      <div class="modal-actions">
        <button onclick={() => positionLb = null}>Cancel</button>
        <button class="primary" onclick={savePositions}>Save</button>
      </div>
    </div>
  </div>
{/if}
