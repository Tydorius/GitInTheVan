<script lang="ts">
  import { api, withScanOverride } from '../api'
  import SnapshotHistoryModal from '../lib/SnapshotHistoryModal.svelte'
  import { downloadJson } from '../lib/download'
  import { onMount } from 'svelte'
  import { routeParams } from '../stores'
  import CodeEditor from '../lib/CodeEditor.svelte'
  import TagEditModal from '../lib/TagEditModal.svelte'
  import { withScroll } from '../lib/scroll'
  import CollapsibleCard from '../lib/CollapsibleCard.svelte'
  import { CollapseController } from '../lib/collapse'

  let collapse = new CollapseController('cantrips', ['tester', 'templates', 'sequence'])

  let collapsedCards: { [key: string]: boolean } = {}

  function toggleCard(id: string) {
    collapsedCards[id] = !(collapsedCards[id] ?? false)
    collapsedCards = { ...collapsedCards }
  }

  function expandAllCards() {
    const expanded: { [key: string]: boolean } = {}
    for (const s of cantrips) {
      expanded[`cantrip-${s.id}`] = false
    }
    collapsedCards = expanded
    collapse.setAll(false)
  }

  function collapseAllCards() {
    const collapsed: { [key: string]: boolean } = {}
    for (const s of cantrips) {
      collapsed[`cantrip-${s.id}`] = true
    }
    collapsedCards = collapsed
    collapse.setAll(true)
  }

  let cantrips: any[] = []
  let loading = true
  let error = ''
  let showForm = false
  let editingId: string | null = null
  let showTest = false
  let testResults: any = null
  let testing = false

  let form = {
    name: '', description: '', llm_instructions: '', code: '',
    hook_type: 'pre', is_active: true, is_public: false,
    run_pre_driver: true, run_driver_callable: false,
    run_pre_navigator: false, run_post_navigator: false,
    execution_order: 10, timeout_ms: 5000, budget_weight: 1.0,
    lorebook_reads_text: '',
    response_tags_text: '',
  }

  let tagModal = { show: false, id: '', name: '', tag: '' }

  let testConfig = {
    selectedCantripId: '',
    messages: '[{"role": "user", "content": "Hello"}]',
    character_name: '',
    chat_data: '{}',
  }

  let showSequence = false
  let seqTesting = false
  let seqResults: any = null
  let seqConfig = {
    selectedCantripId: '',
    conversation_id: '',
    turns: '[\n  {"user_message": "Hello", "reply": "Hi there."}\n]',
    seed_chat_data: '{}',
    start_turn_index: 0,
  }

  function cantripPositions(turn: any): [string, any[]][] {
    return Object.entries(turn?.cantrips ?? {}) as [string, any[]][]
  }

  async function runSequence() {
    error = ''
    let turns: any[]
    let seeds: any
    try {
      turns = JSON.parse(seqConfig.turns)
      if (!Array.isArray(turns)) throw new Error('Turns must be a JSON array')
      seeds = JSON.parse(seqConfig.seed_chat_data || '{}')
      if (typeof seeds !== 'object' || seeds === null || Array.isArray(seeds)) {
        throw new Error('Seed chat data must be a JSON object')
      }
    } catch (e: any) {
      error = `Invalid JSON: ${e.message}`
      return
    }
    seqTesting = true
    seqResults = null
    try {
      seqResults = await api.testCantripSequence({
        turns,
        conversation_id: seqConfig.conversation_id.trim(),
        seed_chat_data: seeds,
        start_turn_index: Number(seqConfig.start_turn_index) || 0,
        ...(seqConfig.selectedCantripId
          ? { only_cantrip_ids: [seqConfig.selectedCantripId] }
          : {}),
      })
    } catch (e: any) { error = e.message }
    finally { seqTesting = false }
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
    form = { name: '', description: '', llm_instructions: '', code: '', hook_type: 'pre', is_active: true, is_public: false, run_pre_driver: true, run_driver_callable: false, run_pre_navigator: false, run_post_navigator: false, execution_order: 10, timeout_ms: 5000, budget_weight: 1.0, lorebook_reads_text: '', response_tags_text: '' }
    editingId = null
    validationStatus = null
    errorLine = null
  }

  function startEdit(s: any) {
    editingId = s.id
    validationStatus = null
    errorLine = null
    form = {
      name: s.name, description: s.description, llm_instructions: s.llm_instructions || '', code: s.code || '',
      hook_type: s.hook_type, is_active: s.is_active, is_public: s.is_public,
      run_pre_driver: s.run_pre_driver ?? true, run_driver_callable: s.run_driver_callable ?? false,
      run_pre_navigator: s.run_pre_navigator ?? false, run_post_navigator: s.run_post_navigator ?? false,
      execution_order: s.execution_order, timeout_ms: s.timeout_ms,
      budget_weight: s.budget_weight ?? 1.0,
      lorebook_reads_text: (s.lorebook_reads || []).join(', '),
      response_tags_text: (s.response_tags || []).join(', '),
    }
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
      // lorebook_reads travels as an array; the form edits it as text.
      const { lorebook_reads_text, response_tags_text, ...rest } = form
      const payload = {
        ...rest,
        lorebook_reads: (lorebook_reads_text || '').split(',').map((s: string) => s.trim()).filter(Boolean),
        response_tags: (response_tags_text || '').split(',').map((s: string) => s.trim().toLowerCase()).filter(Boolean),
        override_scan: false,
      }
      await withScanOverride((overrideScan: boolean) =>
        editingId
          ? api.updateCantrip(editingId, { ...payload, override_scan: overrideScan })
          : api.createCantrip({ ...payload, override_scan: overrideScan }),
      )
      showForm = false; resetForm(); await withScroll(load)
    } catch (e: any) { error = e.message }
  }

  async function handleDelete(id: string) {
    if (!confirm('Delete this cantrip?')) return
    try { await api.deleteCantrip(id); await withScroll(load) }
    catch (e: any) { error = e.message }
  }

  async function toggleActive(s: any) {
    try { await api.updateCantrip(s.id, { is_active: !s.is_active }); await withScroll(load) }
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

  function openTest(cantripId: string | undefined = undefined) {
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

  /**
   * Open the object named by `#/...?id=`.
   *
   * Links from the Debug and comparison views, and from the Packs page, carry
   * the object's id. The query used to be stripped before any page saw it, so
   * those links landed here with nothing selected.
   */
  function openFromRoute() {
    const id = $routeParams.params.id
    if (!id) return
    const match = cantrips.find((item: any) => item.id === id)
    if (match) startEdit(match)
  }

  onMount(async () => {
    await load()
    openFromRoute()
  })

  // A link clicked while this page is already open changes only the hash.
  $: if ($routeParams.params.id && cantrips.length) openFromRoute()

  // Version history (Phase 25). One modal serves every type that keeps history.
  let historyShow = false
  let historyType = 'cantrip'
  let historyId = ''
  let historyName = ''

  function openHistory(item: any, type: string = 'cantrip') {
    historyType = type
    historyId = item.id
    historyName = item.name || ''
    historyShow = true
  }

  async function handleExport(id: string) {
    try {
      const data = await api.exportCantrip(id)
      downloadJson(data, `${data.name || 'cantrip'}.json`)
    } catch (e: any) { error = e.message }
  }
</script>

<div class="page-header">
  <h2>Cantrips <a class="help-link" href="/help/user-guide.html#cantrips" target="_blank" title="Open documentation">?</a></h2>
  <div style="display: flex; gap: 4px; align-items: center;">
    {#if showTemplates || showTest}
      <button onclick={collapseAllCards}>Collapse All</button>
      <button onclick={expandAllCards}>Expand All</button>
      <span style="width: 1px; height: 20px; background: var(--border); margin: 0 4px;"></span>
    {:else}
      <button onclick={collapseAllCards}>Collapse All</button>
      <button onclick={expandAllCards}>Expand All</button>
      <span style="width: 1px; height: 20px; background: var(--border); margin: 0 4px;"></span>
    {/if}
    <button onclick={() => { loadTemplates(); showTemplates = !showTemplates; }}>Templates</button>
    <button onclick={() => { showTest = !showTest; testResults = null; }}>Test Panel</button>
    <button onclick={() => { showSequence = !showSequence; seqResults = null; }}>Sequence Tester</button>
    <button class="primary" onclick={() => { resetForm(); showForm = true; }}>+ Add Cantrip</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

{#if showTemplates}
  <CollapsibleCard title="Cantrip Templates" cardKey="templates" {collapse}>
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
  </CollapsibleCard>
{/if}

{#if showTest}
  <CollapsibleCard title="Cantrip Tester" cardKey="tester" {collapse}>
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
  </CollapsibleCard>
{/if}

{#if showSequence}
  <CollapsibleCard title="Sequence Tester" cardKey="sequence" {collapse}>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 12px;">
      Drive a script of turns (user message + canned reply) through the real cantrip pipeline with state chained in memory — ladders, clocks and chains over many turns, no LLM calls, nothing persisted.
    </p>
    <div class="form-group">
      <label for="seq-scope">Cantrips to Run</label>
      <select id="seq-scope" bind:value={seqConfig.selectedCantripId}>
        <option value="">All (real tag/Active activation)</option>
        {#each cantrips as s}<option value={s.id}>{s.name}</option>{/each}
      </select>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label for="seq-conv">Conversation Label (optional)</label>
        <input id="seq-conv" bind:value={seqConfig.conversation_id} placeholder="tester: my-scenario" />
      </div>
      <div class="form-group">
        <label for="seq-turn">Start Turn Index</label>
        <input id="seq-turn" type="number" bind:value={seqConfig.start_turn_index} />
      </div>
    </div>
    <div class="form-group">
      <label for="seq-turns">Turns (JSON array)</label>
      <textarea id="seq-turns" bind:value={seqConfig.turns} style="min-height: 120px; font-family: monospace;"></textarea>
    </div>
    <div class="form-group">
      <label for="seq-seeds">Seed Chat Data (JSON object)</label>
      <input id="seq-seeds" bind:value={seqConfig.seed_chat_data} placeholder={'{}'} />
    </div>
    <button class="primary" onclick={runSequence} disabled={seqTesting}>
      {seqTesting ? 'Running...' : 'Run Sequence'}
    </button>

    {#if seqResults}
      <div style="margin-top: 16px;">
        {#if seqResults.stop_reason}
          <div class="error-msg">Stopped at turn {seqResults.stopped_at_turn}: {seqResults.stop_reason}</div>
        {/if}
        {#each seqResults.turns as turn (turn.index)}
          <details style="margin-bottom: 8px; border: 1px solid var(--border); border-radius: 4px; padding: 8px;">
            <summary style="cursor: pointer;">
              <strong>Turn {turn.index}</strong>
              {#if turn.tags?.length}<span class="badge" style="margin-left: 6px;">tags: {turn.tags.join(', ')}</span>{/if}
              {#if turn.is_regenerate}<span class="badge" style="margin-left: 6px;">regenerate</span>{/if}
            </summary>
            <div style="margin-top: 8px;">
              {#each cantripPositions(turn) as [position, records] (position)}
                {#if records.length}
                  <div class="form-group">
                    <label>{position}</label>
                    {#each records as r}
                      <div style="font-size: 12px; padding: 2px 0; {r.triggered ? '' : 'opacity: 0.6;'}">
                        {r.triggered ? '▶' : '·'} {r.name}
                        {#if r.error}<span style="color: var(--danger, #c0392b);"> — {r.error}</span>{/if}
                        {#if !r.triggered && r.reason}<span style="color: var(--text-dim);"> — {r.reason}</span>{/if}
                      </div>
                      {#if r.debug_logs?.length}
                        <div class="debug-output" style="font-size: 11px;">{r.debug_logs.join('\n')}</div>
                      {/if}
                    {/each}
                  </div>
                {/if}
              {/each}
              {#if Object.keys(turn.response_tags || {}).length}
                <div class="form-group"><label>Response Tags</label><div class="debug-output">{JSON.stringify(turn.response_tags)}</div></div>
              {/if}
              <div class="form-group"><label>Reply (client-visible)</label><div class="debug-output">{turn.reply}</div></div>
              {#if Object.keys(turn.chat_data || {}).length}
                <div class="form-group"><label>Chat Data After Turn</label><div class="debug-output">{JSON.stringify(turn.chat_data, null, 2)}</div></div>
              {/if}
            </div>
          </details>
        {/each}
        {#if Object.keys(seqResults.final_chat_data || {}).length}
          <div class="form-group"><label>Final Chat Data</label><div class="debug-output">{JSON.stringify(seqResults.final_chat_data, null, 2)}</div></div>
        {/if}
        {#if Object.keys(seqResults.final_user_data || {}).length}
          <div class="form-group"><label>Final User Data</label><div class="debug-output">{JSON.stringify(seqResults.final_user_data, null, 2)}</div></div>
        {/if}
        {#if Object.keys(seqResults.final_cantrip_data || {}).length}
          <div class="form-group"><label>Final Cantrip Data</label><div class="debug-output">{JSON.stringify(seqResults.final_cantrip_data, null, 2)}</div></div>
        {/if}
      </div>
    {/if}
  </CollapsibleCard>
{/if}

{#if loading}<div class="loading">Loading...</div>
{:else if cantrips.length === 0 && !showForm}
  <div class="empty-state">No cantrips configured.</div>
{:else}
  {#each cantrips as s}
    <div class="card" data-scroll-anchor={s.id}>
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
          <button onclick={() => openHistory(s)}>History</button>
          <button onclick={() => handleExport(s.id)}>Export</button>
          <button class="danger" onclick={() => handleDelete(s.id)}>Delete</button>
          <button onclick={() => toggleCard(`cantrip-${s.id}`)} style="padding: 2px 4px; font-size: 14px; border: none; background: none; cursor: pointer;" title={(collapsedCards[`cantrip-${s.id}`] ?? false) ? 'Expand' : 'Collapse'}>
            {(collapsedCards[`cantrip-${s.id}`] ?? false) ? '▶' : '▼'}
          </button>
        </div>
      </div>
      {#if !(collapsedCards[`cantrip-${s.id}`] ?? false)}
        {#if s.description}<p style="color: var(--text-dim); font-size: 12px; margin-bottom: 8px;">{s.description}</p>{/if}
        <div style="color: var(--text-dim); font-size: 11px;">
          Hook: {s.hook_type} | Order: {s.execution_order} | Timeout: {s.timeout_ms}ms
        </div>
      {/if}
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
            <label for="cantrip-desc">Description</label>
            <input id="cantrip-desc" autocomplete="off" bind:value={form.description} />
          </div>
        </div>
        <div class="form-group">
          <label>Pipeline Positions</label>
          <div style="display: flex; flex-wrap: wrap; gap: 12px; margin-top: 4px;">
            <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
              <input type="checkbox" bind:checked={form.run_pre_driver} style="width: auto;"> Pre-Driver
            </label>
            <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
              <input type="checkbox" bind:checked={form.run_driver_callable} style="width: auto;"> Driver-Callable
            </label>
            <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
              <input type="checkbox" bind:checked={form.run_pre_navigator} style="width: auto;"> Pre-Navigator
            </label>
            <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
              <input type="checkbox" bind:checked={form.run_post_navigator} style="width: auto;"> Post-Navigator
            </label>
          </div>
          <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            Select when this cantrip runs. Pre-Driver: before the writing LLM. Pre-Navigator: after response, before verification (can modify response). Post-Navigator: after verification, final cleanup.
          </p>
        </div>
        <div class="form-group">
          <label for="cantrip-llm">LLM Instructions (for Driver-Callable tool notifications)</label>
          <textarea id="cantrip-llm" bind:value={form.llm_instructions} rows="3"
            placeholder="Instructions the LLM sees when this cantrip is available as a tool. E.g., 'Call this to roll dice. Args: count (number of dice), sides (number of sides).'"
            style="width: 100%; font-family: monospace; font-size: 12px; resize: vertical;"></textarea>
          <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            When this cantrip is in the Driver-Callable position, this text is shown to the writing LLM. Falls back to Description if empty.
          </p>
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
          <div class="form-group">
            <label for="cantrip-budget">Budget Weight</label>
            <input id="cantrip-budget" type="number" step="0.1" min="0" bind:value={form.budget_weight} />
          </div>
        </div>
        <div class="form-row">
          <div class="form-group" style="flex: 1;">
            <label for="cantrip-reads">Lorebook Reads (comma-separated lorebook names)</label>
            <input id="cantrip-reads" type="text" bind:value={form.lorebook_reads_text}
              placeholder="e.g. World Codex, Faction Notes" />
            <small>Entries from these lorebooks are seeded read-only into the script's
              <code>context.lorebook</code> (max 20 books, capped by the admin lorebook size limit).</small>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group" style="flex: 1;">
            <label for="cantrip-response-tags">Response Tags (comma-separated tag names)</label>
            <input id="cantrip-response-tags" type="text" bind:value={form.response_tags_text}
              placeholder="e.g. deed, loot" />
            <small>GitV extracts these tags from the model's reply, strips them before the
              client, and hands the contents to post-Navigator scripts the same turn via
              <code>context.response_tags</code>. Lowercase letters, digits, - and _; unique per user.</small>
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
          <button type="button" onclick={() => { showForm = false; resetForm(); }}>Cancel</button>
          <button type="submit" class="primary">{editingId ? 'Update' : 'Create'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<SnapshotHistoryModal
  bind:show={historyShow}
  resourceType={historyType}
  resourceId={historyId}
  resourceName={historyName}
  onRestored={() => withScroll(load)}
/>
