<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'
  import CodeEditor from '../lib/CodeEditor.svelte'
  import TagEditModal from '../lib/TagEditModal.svelte'

  let rules: any[] = []
  let endpoints: any[] = []
  let logs: any[] = []
  let loading = true
  let error = ''
  let showForm = false
  let editingId: string | null = null
  let tab = 'rules'

  let form = {
    name: '', description: '', prompt: '',
    is_active: true, max_retries: 2, execution_order: 10,
    resubmission_strategy: 'add_instructions',
    verification_endpoint_id: null as string | null,
    verification_model: '',
  }

  let tagModal = { show: false, id: '', name: '', tag: '' }
  let tagError = ''

  let vSettings = { verification_enabled: false, verification_endpoint_id: '' as string | null, verification_model: '' }

  let testForm = { content: '', prompt: '', rule_id: '', endpoint_id: '', model: '' }
  let testResult: any = null
  let testing = false

  let forbiddenSettings = { forbidden_words_enabled: false, forbidden_words_case_sensitive: false }
  let forbiddenWords: any[] = []
  let newForbiddenPhrase = ''
  let forbiddenTestContent = ''
  let forbiddenTestResult: any = null
  let forbiddenTesting = false

  async function load() {
    loading = true
    try {
      const [r, e, s, l, fs, fw] = await Promise.all([
        api.listVerificationRules(), api.listEndpoints(),
        api.getVerificationSettings(), api.listVerificationLogs(),
        api.getForbiddenSettings(), api.listForbiddenWords(),
      ])
      rules = r.rules
      endpoints = e.endpoints
      vSettings = s
      logs = l.logs
      forbiddenSettings = fs
      forbiddenWords = fw.words
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  function resetForm() {
    form = { name: '', description: '', prompt: '', is_active: true, max_retries: 2, execution_order: 10, resubmission_strategy: 'add_instructions', verification_endpoint_id: null, verification_model: '' }
    editingId = null
  }

  function startEdit(r: any) {
    editingId = r.id
    form = { name: r.name, description: r.description, prompt: r.prompt, is_active: r.is_active, max_retries: r.max_retries, execution_order: r.execution_order, resubmission_strategy: r.resubmission_strategy, verification_endpoint_id: r.verification_endpoint_id || null, verification_model: r.verification_model || '' }
    showForm = true
  }

  async function handleSubmit() {
    error = ''
    try {
      if (editingId) await api.updateVerificationRule(editingId, form)
      else await api.createVerificationRule(form)
      showForm = false; resetForm(); await load()
    } catch (e: any) { error = e.message }
  }

  async function handleDelete(id: string) {
    if (!confirm('Delete this rule?')) return
    try { await api.deleteVerificationRule(id); await load() }
    catch (e: any) { error = e.message }
  }

  async function toggleRuleActive(r: any) {
    try { await api.updateVerificationRule(r.id, { is_active: !r.is_active }); await load() }
    catch (e: any) { error = e.message }
  }

  function openTagModal(r: any) {
    tagError = ''
    tagModal = { show: true, id: r.id, name: r.name, tag: r.tag || '' }
  }

  async function saveTag(tag: string) {
    try { await api.updateVerificationRule(tagModal.id, { tag }); tagModal.show = false; await load() }
    catch (e: any) { tagError = e.message }
  }

  async function saveSettings() {
    error = ''
    try { await api.updateVerificationSettings(vSettings); }
    catch (e: any) { error = e.message }
  }

  async function runTest() {
    testing = true; testResult = null; error = ''
    try {
      testResult = await api.testVerification({
        content: testForm.content,
        prompt: testForm.prompt || undefined,
        rule_id: testForm.rule_id || undefined,
        endpoint_id: testForm.endpoint_id || undefined,
        model: testForm.model || undefined,
      })
    } catch (e: any) { error = e.message }
    finally { testing = false }
  }

  async function saveForbiddenSettings() {
    error = ''
    try { forbiddenSettings = await api.updateForbiddenSettings(forbiddenSettings) }
    catch (e: any) { error = e.message }
  }

  async function addForbiddenWord() {
    if (!newForbiddenPhrase.trim()) return
    error = ''
    try {
      await api.createForbiddenWord(newForbiddenPhrase.trim())
      newForbiddenPhrase = ''
      forbiddenWords = (await api.listForbiddenWords()).words
    } catch (e: any) { error = e.message }
  }

  async function deleteForbiddenWord(id: string) {
    try {
      await api.deleteForbiddenWord(id)
      forbiddenWords = (await api.listForbiddenWords()).words
    } catch (e: any) { error = e.message }
  }

  async function testForbidden() {
    forbiddenTesting = true; forbiddenTestResult = null; error = ''
    try {
      forbiddenTestResult = await api.testForbiddenWords(forbiddenTestContent)
    } catch (e: any) { error = e.message }
    finally { forbiddenTesting = false }
  }

  let autoRefresh = false
  let refreshTimer: ReturnType<typeof setInterval> | null = null

  async function refreshLogs() {
    try {
      const l = await api.listVerificationLogs()
      logs = l.logs
    } catch {}
  }

  function toggleAutoRefresh() {
    autoRefresh = !autoRefresh
    if (autoRefresh) {
      refreshTimer = setInterval(refreshLogs, 15000)
    } else if (refreshTimer) {
      clearInterval(refreshTimer)
      refreshTimer = null
    }
  }

  $: if (tab === 'logs') refreshLogs()

  onMount(load)
</script>

<div class="page-header">
  <h2>Verification <a class="help-link" href="/help/user-guide.html#verification" target="_blank" title="Open documentation">?</a></h2>
  <div>
    <button onclick={() => tab = 'rules'} class={tab === 'rules' ? 'primary' : ''}>Rules</button>
    <button onclick={() => tab = 'settings'} class={tab === 'settings' ? 'primary' : ''}>Settings</button>
    <button onclick={() => tab = 'logs'} class={tab === 'logs' ? 'primary' : ''}>Logs</button>
    <button onclick={() => tab = 'forbidden'} class={tab === 'forbidden' ? 'primary' : ''}>Forbidden Words</button>
    <button onclick={() => tab = 'test'} class={tab === 'test' ? 'primary' : ''}>Test</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

{#if tab === 'rules'}
  {#if loading}<div class="loading">Loading...</div>
  {:else}
    <div style="margin-bottom: 16px;">
      <button class="primary" onclick={() => { resetForm(); showForm = true; }}>+ Add Rule</button>
    </div>
    {#if rules.length === 0}<div class="empty-state">No verification rules configured.</div>
    {:else}
      {#each rules as r}
        <div class="card">
          <div class="card-header">
            <div>
              <strong>{r.name}</strong>
              {#if r.tag}
                <span style="display: inline-flex; align-items: center; gap: 4px; margin-left: 8px;">
                  <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; cursor: pointer;"
                        onclick={() => navigator.clipboard.writeText('<#verify-' + r.tag + '#>')}
                        title="Click to copy">&lt;#verify-{r.tag}#&gt;</span>
                  <button onclick={() => openTagModal(r)} style="padding: 0 4px; font-size: 14px; border: none; background: none; cursor: pointer;" title="Edit tag">🖉</button>
                </span>
              {:else}
                <button onclick={() => openTagModal(r)} style="margin-left: 8px; padding: 2px 8px; font-size: 11px;">+ Tag</button>
              {/if}
            </div>
            <div>
              <button
                onclick={() => toggleRuleActive(r)}
                style="padding: 2px 8px; font-size: 11px;"
                class={r.is_active ? 'primary' : ''}
              >{r.is_active ? 'ON' : 'OFF'}</button>
              <button onclick={() => startEdit(r)}>Edit</button>
              <button class="danger" onclick={() => handleDelete(r.id)}>Delete</button>
            </div>
          </div>
          <p style="color: var(--text-dim); font-size: 12px;">{r.description || 'No description'}</p>
          <div style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            Max retries: {r.max_retries} | Strategy: {r.resubmission_strategy} | Order: {r.execution_order}
          </div>
        </div>
      {/each}
    {/if}
  {/if}

{:else if tab === 'settings'}
  <div class="card">
    <h3>Verification Settings</h3>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Configure which endpoint and model is used for response verification.
    </p>
    <div class="form-group">
      <label><input type="checkbox" bind:checked={vSettings.verification_enabled} style="width: auto;"> Verification Enabled</label>
    </div>
    <div class="form-group">
      <label>Verification Endpoint</label>
      <select bind:value={vSettings.verification_endpoint_id}>
        <option value="">Select endpoint...</option>
        {#each endpoints as ep}<option value={ep.id}>{ep.name} ({ep.base_url})</option>{/each}
      </select>
    </div>
    <div class="form-group">
      <label>Verification Model</label>
      <input bind:value={vSettings.verification_model} placeholder="Gemma-4-31B-it" />
    </div>
    <button class="primary" onclick={saveSettings}>Save Settings</button>
  </div>

{:else if tab === 'logs'}
  <div style="margin-bottom: 12px;">
    <button onclick={refreshLogs}>Refresh</button>
    <button onclick={toggleAutoRefresh} class={autoRefresh ? 'primary' : ''}>{autoRefresh ? 'Auto ON (15s)' : 'Auto OFF'}</button>
  </div>
  {#if logs.length === 0}<div class="empty-state">No verification logs yet.</div>
  {:else}
    <table>
      <thead><tr><th>Rule</th><th>Result</th><th>Severity</th><th>Retries</th><th>Reason</th><th>Time</th></tr></thead>
      <tbody>
        {#each logs.slice(0, 30) as log}
          <tr>
            <td>{log.rule_name}</td>
            <td>{#if log.approved}<span class="badge approved">Approved</span>{:else}<span class="badge violation">Rejected</span>{/if}</td>
            <td>{log.severity}</td>
            <td>{log.retries_used}</td>
            <td style="max-width: 300px; overflow: hidden; text-overflow: ellipsis;">{log.violation_reason || '-'}</td>
            <td style="color: var(--text-dim); font-size: 11px;">{new Date(log.created_at).toLocaleString()}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

{:else if tab === 'forbidden'}
  <div class="card">
    <h3>Forbidden Words &amp; Phrases</h3>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Phrases checked case-insensitively against the Driver's response before the Navigator runs. Matches are surfaced to the Navigator as concrete violations. Works with or without verification rules.
    </p>
    <div class="form-group">
      <label>
        <input type="checkbox" bind:checked={forbiddenSettings.forbidden_words_enabled} style="width: auto;">
        Enable Forbidden Words Check
      </label>
    </div>
    <div class="form-group">
      <label>
        <input type="checkbox" bind:checked={forbiddenSettings.forbidden_words_case_sensitive} style="width: auto;">
        Case Sensitive
      </label>
    </div>
    <button class="primary" onclick={saveForbiddenSettings}>Save Settings</button>
  </div>

  <div class="card">
    <h3>Forbidden Phrases</h3>
    <div style="display: flex; gap: 8px; margin-bottom: 12px;">
      <input bind:value={newForbiddenPhrase} placeholder="Enter a word or phrase..." onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addForbiddenWord(); } }} />
      <button class="primary" onclick={addForbiddenWord} disabled={!newForbiddenPhrase.trim()}>Add</button>
    </div>
    {#if forbiddenWords.length === 0}
      <div class="empty-state" style="padding: 16px;">No forbidden phrases configured.</div>
    {:else}
      <table>
        <thead><tr><th>Phrase</th><th>Regex</th><th>Actions</th></tr></thead>
        <tbody>
          {#each forbiddenWords as w}
            <tr>
              <td>{w.phrase}</td>
              <td>{#if w.is_regex}<span class="badge approved">Yes</span>{:else}No{/if}</td>
              <td><button class="danger" onclick={() => deleteForbiddenWord(w.id)} style="font-size: 12px;">Delete</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>

  <div class="card">
    <h3>Test Forbidden Words</h3>
    <div class="form-group">
      <textarea bind:value={forbiddenTestContent} placeholder="Paste response text to check..." style="min-height: 80px;"></textarea>
    </div>
    <button class="primary" onclick={testForbidden} disabled={forbiddenTesting || !forbiddenTestContent.trim()}>
      {forbiddenTesting ? 'Scanning...' : 'Scan'}
    </button>
    {#if forbiddenTestResult}
      <div style="margin-top: 12px;">
        {#if forbiddenTestResult.has_matches}
          <div class="error-msg">
            {forbiddenTestResult.match_count} phrase(s) matched.
            <pre style="margin-top: 8px; white-space: pre-wrap;">{forbiddenTestResult.summary}</pre>
          </div>
        {:else}
          <div class="success-msg">No forbidden phrases found.</div>
        {/if}
      </div>
    {/if}
  </div>

{:else if tab === 'test'}
  <div class="card">
    <h3>Verification Tester</h3>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">Test a verification prompt against sample response content.</p>
    <div class="form-group">
      <label>Response Content to Check</label>
      <textarea bind:value={testForm.content} placeholder="Paste the LLM response to verify..." style="min-height: 100px;"></textarea>
    </div>
    <div class="form-group">
      <label>Use Rule (optional — loads rule prompt automatically)</label>
      <select bind:value={testForm.rule_id}>
        <option value="">No rule — use custom prompt below</option>
        {#each rules as r}<option value={r.id}>{r.name}</option>{/each}
      </select>
    </div>
    <div class="form-group">
      <label>Verification Prompt (ignored if rule selected above)</label>
      <textarea bind:value={testForm.prompt} placeholder="The character must never mention being an AI..."></textarea>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Endpoint</label>
        <select bind:value={testForm.endpoint_id}>
          <option value="">Use default verification endpoint</option>
          {#each endpoints as ep}<option value={ep.id}>{ep.name}</option>{/each}
        </select>
      </div>
      <div class="form-group">
        <label>Model</label>
        <input bind:value={testForm.model} placeholder="Leave blank for default" />
      </div>
    </div>
    <button class="primary" onclick={runTest} disabled={testing}>{testing ? 'Checking...' : 'Run Verification Check'}</button>

    {#if testResult}
      <div style="margin-top: 16px;">
        {#if testResult.violation}
          <div class="error-msg">VIOLATION DETECTED: {testResult.reason} (severity: {testResult.severity})</div>
        {:else}
          <div class="success-msg">Response approved - no violations detected.</div>
        {/if}
        {#if testResult.thinking}
          <div style="margin-top: 12px;">
            <h4 style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">Model Thinking</h4>
            <div style="background: var(--bg-elevated); padding: 12px; border-radius: 4px; white-space: pre-wrap; font-size: 12px; max-height: 300px; overflow-y: auto; font-family: monospace; border-left: 3px solid var(--accent);">
              {testResult.thinking}
            </div>
          </div>
        {/if}
        {#if testResult.raw_response}
          <details style="margin-top: 12px;">
            <summary style="font-size: 12px; color: var(--text-dim); cursor: pointer;">Raw LLM Response</summary>
            <div style="background: var(--bg-elevated); padding: 12px; border-radius: 4px; white-space: pre-wrap; font-size: 11px; max-height: 200px; overflow-y: auto; font-family: monospace; margin-top: 8px;">
              {testResult.raw_response}
            </div>
          </details>
        {/if}
      </div>
    {/if}
  </div>
{/if}

{#if showForm}
  <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) showForm = false; }}>
    <div class="modal" style="width: 600px;">
      <h3>{editingId ? 'Edit Rule' : 'Add Verification Rule'}</h3>
      <form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
        <div class="form-group"><label>Name</label><input bind:value={form.name} required /></div>
        <div class="form-group"><label>Description</label><input bind:value={form.description} /></div>
        <div class="form-group">
          <label>Verification Prompt</label>
          <CodeEditor bind:value={form.prompt} language="markdown" minHeight="150px" placeholder="The response must stay in character at all times. The character must never mention being an AI..." />
        </div>
        <div class="form-row">
          <div class="form-group"><label>Max Retries</label><input type="number" bind:value={form.max_retries} /></div>
          <div class="form-group"><label>Execution Order</label><input type="number" bind:value={form.execution_order} /></div>
        </div>
        <div class="form-group">
          <label>Resubmission Strategy</label>
          <select bind:value={form.resubmission_strategy}>
            <option value="add_instructions">Add Instructions (append correction)</option>
            <option value="rewrite">Rewrite (send bad response for rewriting)</option>
          </select>
        </div>
        <div class="form-group"><label><input type="checkbox" bind:checked={form.is_active} style="width: auto;"> Active</label></div>
        <div class="form-row">
          <div class="form-group">
            <label>Verification Endpoint Override</label>
            <select bind:value={form.verification_endpoint_id}>
              <option value={null}>Use global setting</option>
              {#each endpoints as ep}<option value={ep.id}>{ep.name} ({ep.base_url})</option>{/each}
            </select>
          </div>
          <div class="form-group">
            <label>Verification Model Override</label>
            <input bind:value={form.verification_model} placeholder="Leave blank for global" />
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

<TagEditModal
  bind:show={tagModal.show}
  resourceType="verify"
  resourceName={tagModal.name}
  currentTag={tagModal.tag}
  bind:errorMsg={tagError}
  onSave={saveTag}
/>
