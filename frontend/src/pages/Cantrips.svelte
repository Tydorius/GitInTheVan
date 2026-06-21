<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'
  import CodeEditor from '../lib/CodeEditor.svelte'
  import TagEditModal from '../lib/TagEditModal.svelte'

  let cantrips: any[] = []
  let loading = true
  let error = ''
  let showForm = false
  let editingId: string | null = null
  let showTest = false
  let testResults: any = null
  let testing = false

  let form = {
    name: '', description: '', code: '',
    hook_type: 'pre', is_active: true, is_public: false,
    execution_order: 10, timeout_ms: 5000,
  }

  let tagModal = { show: false, id: '', name: '', tag: '' }

  let testConfig = {
    selectedCantripId: '',
    messages: '[{"role": "user", "content": "Hello"}]',
    character_name: '',
    chat_data: '{}',
  }

  async function load() {
    loading = true
    try {
      const data = await api.listCantrips()
      cantrips = data.cantrips
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  function resetForm() {
    form = { name: '', description: '', code: '', hook_type: 'pre', is_active: true, is_public: false, execution_order: 10, timeout_ms: 5000 }
    editingId = null
  }

  function startEdit(s: any) {
    editingId = s.id
    form = { name: s.name, description: s.description, code: s.code || '', hook_type: s.hook_type, is_active: s.is_active, is_public: s.is_public, execution_order: s.execution_order, timeout_ms: s.timeout_ms }
    showForm = true
  }

  function openTagModal(s: any) {
    tagModal = { show: true, id: s.id, name: s.name, tag: s.tag || '' }
  }

  async function saveTag(tag: string) {
    try { await api.updateCantrip(tagModal.id, { tag }); tagModal.show = false; await load() }
    catch (e: any) { tagError = e.message }
  }
  let tagError = ''

  let validationStatus: { valid: boolean; error: string | null } | null = null
  let validating = false
  let errorLine: number | null = null

  function parseErrorLine(error: string): number | null {
    const match = error.match(/(?:line\s+|<anonymous>:)(\d+)/i)
    if (match) return parseInt(match[1])
    return null
  }

  async function validateCode() {
    if (!form.code.trim()) return
    validating = true; validationStatus = null; errorLine = null
    try {
      validationStatus = await api.validateCantrip(form.code)
      if (validationStatus.error) {
        errorLine = parseErrorLine(validationStatus.error)
      }
    } catch (e: any) {
      validationStatus = { valid: false, error: e.message }
    } finally { validating = false }
  }

  async function handleSubmit() {
    error = ''
    try {
      if (editingId) { await api.updateCantrip(editingId, form) }
      else { await api.createCantrip(form) }
      showForm = false; resetForm(); await load()
    } catch (e: any) { error = e.message }
  }

  async function handleDelete(id: string) {
    if (!confirm('Delete this cantrip?')) return
    try { await api.deleteCantrip(id); await load() }
    catch (e: any) { error = e.message }
  }

  async function toggleActive(s: any) {
    try { await api.updateCantrip(s.id, { is_active: !s.is_active }); await load() }
    catch (e: any) { error = e.message }
  }

  let templates: any[] = []
  let showTemplates = false

  async function loadTemplates() {
    try { const data = await api.listTemplates(); templates = data.templates }
    catch (e: any) { error = e.message }
  }

  async function installTemplate(name: string) {
    error = ''
    try {
      await api.installTemplate(name)
      showTemplates = false
      await load()
    } catch (e: any) { error = e.message }
  }

  function openTest(cantripId?: string) {
    testConfig.selectedCantripId = cantripId || ''
    showTest = true
    testResults = null
  }

  async function runTest() {
    testing = true; testResults = null; error = ''
    try {
      const messages = JSON.parse(testConfig.messages)
      const chat_data = testConfig.chat_data.trim() ? JSON.parse(testConfig.chat_data) : undefined

      if (testConfig.selectedCantripId) {
        const res = await api.testCantripById(testConfig.selectedCantripId, {
          messages,
          character_name: testConfig.character_name || undefined,
          chat_data,
        })
        testResults = res
      } else {
        error = 'Select a cantrip to test'
        testing = false
        return
      }
    } catch (e: any) {
      if (e.message) error = e.message
      else error = 'Test failed - check JSON syntax'
    } finally { testing = false }
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Cantrips</h2>
  <div>
    <button onclick={() => { loadTemplates(); showTemplates = !showTemplates; }}>Templates</button>
    <button onclick={() => { showTest = !showTest; testResults = null; }}>Test Panel</button>
    <button class="primary" onclick={() => { resetForm(); showForm = true; }}>+ Add Cantrip</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

{#if showTemplates}
  <div class="card">
    <h3>Cantrip Templates</h3>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">Install pre-built cantrips with one click. You can customize them after installation.</p>
    {#if templates.length === 0}
      <div class="loading">Loading templates...</div>
    {:else}
      {#each templates as t}
        <div class="card" style="margin-bottom: 8px; border-color: var(--border);">
          <div class="card-header">
            <div>
              <strong>{t.name}</strong>
            </div>
            <button class="primary" onclick={() => installTemplate(t.name)} style="font-size: 12px; padding: 4px 12px;">Install</button>
          </div>
          <p style="color: var(--text-dim); font-size: 12px;">{t.description}</p>
        </div>
      {/each}
    {/if}
  </div>
{/if}

{#if showTest}
  <div class="card">
    <h3>Cantrip Tester</h3>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">Run a saved cantrip against test context without forwarding to an LLM.</p>
    <div class="form-group">
      <label for="test-script">Cantrip to Test</label>
      <select id="test-script" bind:value={testConfig.selectedCantripId}>
        <option value="">Select a cantrip...</option>
        {#each cantrips as s}<option value={s.id}>{s.name}</option>{/each}
      </select>
    </div>
    <div class="form-group">
      <label for="test-messages">Messages (JSON)</label>
      <textarea id="test-messages" bind:value={testConfig.messages} style="min-height: 60px;"></textarea>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label for="test-char">Character Name</label>
        <input id="test-char" bind:value={testConfig.character_name} placeholder="Aria" />
      </div>
      <div class="form-group">
        <label for="test-chatdata">Chat Data (JSON)</label>
        <input id="test-chatdata" bind:value={testConfig.chat_data} placeholder="day: 1" />
      </div>
    </div>
    <button class="primary" onclick={runTest} disabled={testing || !testConfig.selectedCantripId}>
      {testing ? 'Running...' : 'Run Test'}
    </button>

    {#if testResults}
      <div style="margin-top: 16px;">
        {#if testResults.error}
          <div class="error-msg">Error: {testResults.error}</div>
        {/if}
        {#if testResults.scenario}
          <div class="form-group"><label>Scenario Output</label><div class="debug-output">{testResults.scenario}</div></div>
        {/if}
        {#if testResults.personality}
          <div class="form-group"><label>Personality Output</label><div class="debug-output">{testResults.personality}</div></div>
        {/if}
        {#if testResults.debug_logs?.length}
          <div class="form-group"><label>Debug Logs</label><div class="debug-output">{testResults.debug_logs.join('\n')}</div></div>
        {/if}
        {#if testResults.chat_data && Object.keys(testResults.chat_data).length > 0}
          <div class="form-group"><label>Chat Data Result</label><div class="debug-output">{JSON.stringify(testResults.chat_data, null, 2)}</div></div>
        {/if}
      </div>
    {/if}
  </div>
{/if}

{#if loading}<div class="loading">Loading...</div>
{:else if cantrips.length === 0 && !showForm}
  <div class="empty-state">No cantrips configured.</div>
{:else}
  {#each cantrips as s}
    <div class="card">
      <div class="card-header">
        <div>
          <strong>{s.name}</strong>
          {#if s.is_public}<span class="badge active" style="margin-left: 4px;">Public</span>{/if}
          {#if s.tag}
            <span style="margin-left: 8px; display: inline-flex; align-items: center; gap: 4px;">
              <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; cursor: pointer;"
                    onclick={() => navigator.clipboard.writeText('<#cantrip-' + s.tag + '#>')}
                    title="Click to copy">&lt;#cantrip-{s.tag}#&gt;</span>
              <button onclick={() => openTagModal(s)} style="padding: 0 4px; font-size: 14px; border: none; background: none; cursor: pointer;" title="Edit tag">🖉</button>
            </span>
          {:else}
            <button onclick={() => openTagModal(s)} style="margin-left: 8px; padding: 2px 8px; font-size: 11px;">+ Tag</button>
          {/if}
        </div>
        <div>
          <button
            onclick={() => toggleActive(s)}
            style="padding: 2px 8px; font-size: 11px;"
            class={s.is_active ? 'primary' : ''}
          >{s.is_active ? 'ON' : 'OFF'}</button>
          <button onclick={() => openTest(s.id)}>Test</button>
          <button onclick={() => startEdit(s)}>Edit</button>
          <button class="danger" onclick={() => handleDelete(s.id)}>Delete</button>
        </div>
      </div>
      {#if s.description}<p style="color: var(--text-dim); font-size: 12px; margin-bottom: 8px;">{s.description}</p>{/if}
      <div style="color: var(--text-dim); font-size: 11px;">
        Hook: {s.hook_type} | Order: {s.execution_order} | Timeout: {s.timeout_ms}ms
      </div>
    </div>
  {/each}
{/if}

<TagEditModal
  bind:show={tagModal.show}
  resourceType="cantrip"
  resourceName={tagModal.name}
  currentTag={tagModal.tag}
  bind:errorMsg={tagError}
  onSave={saveTag}
/>

{#if showForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showForm = false; }}>
    <div class="modal" style="width: 700px;">
      <h3>{editingId ? 'Edit Cantrip' : 'Add Cantrip'}</h3>
      {#if error}<div class="error-msg">{error}</div>{/if}
      <form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
        <div class="form-row">
          <div class="form-group">
            <label for="cantrip-name">Name</label>
            <input id="cantrip-name" autocomplete="off" bind:value={form.name} required />
          </div>
          <div class="form-group">
            <label for="cantrip-hook">Hook Type</label>
            <select id="cantrip-hook" bind:value={form.hook_type}>
              <option value="pre">pre</option>
              <option value="post">post</option>
            </select>
          </div>
        </div>
        <div class="form-group">
          <label for="cantrip-desc">Description</label>
          <input id="cantrip-desc" autocomplete="off" bind:value={form.description} />
        </div>
        <div class="form-group">
          <label for="cantrip-code">JavaScript Code</label>
          <CodeEditor
            bind:value={form.code}
            language="javascript"
            placeholder="context.character.scenario += ' Hello world';"
            minHeight="250px"
            {errorLine}
          />
          <div style="margin-top: 8px; display: flex; gap: 8px; align-items: center;">
            <button type="button" onclick={validateCode} disabled={validating || !form.code.trim()} style="font-size: 12px; padding: 4px 12px;">
              {validating ? 'Checking...' : 'Validate Syntax'}
            </button>
            {#if validationStatus}
              {#if validationStatus.valid}
                <span style="color: var(--success); font-size: 12px;">✓ Valid syntax</span>
              {:else}
                <span style="color: var(--danger); font-size: 12px;">✗ {validationStatus.error}</span>
              {/if}
            {/if}
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label for="cantrip-order">Execution Order</label>
            <input id="cantrip-order" type="number" bind:value={form.execution_order} />
          </div>
          <div class="form-group">
            <label for="cantrip-timeout">Timeout (ms)</label>
            <input id="cantrip-timeout" type="number" bind:value={form.timeout_ms} />
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label><input type="checkbox" bind:checked={form.is_active} style="width: auto;"> Active</label>
          </div>
          <div class="form-group">
            <label><input type="checkbox" bind:checked={form.is_public} style="width: auto;"> Public</label>
          </div>
        </div>
        <div class="modal-actions">
          <button onclick={() => { showForm = false; resetForm(); }}>Cancel</button>
          <button type="submit" class="primary">{editingId ? 'Update' : 'Create'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}
