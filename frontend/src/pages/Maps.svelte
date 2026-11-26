<script lang="ts">
  import { api, withScanOverride } from '../api'
  import ParameterEditor from '../lib/ParameterEditor.svelte'
  import ModelSelect from '../lib/ModelSelect.svelte'
  import SnapshotHistoryModal from '../lib/SnapshotHistoryModal.svelte'
  import { onMount } from 'svelte'
  import { routeParams } from '../stores'
  import { downloadJson } from '../lib/download'
  import CodeEditor from '../lib/CodeEditor.svelte'
  import { withScroll } from '../lib/scroll'

  let maps: any[] = []
  let loading = true
  let error = ''
  let duplicatingId: string | null = null
  let selectedMap: any = null
  let editing = false
  let endpoints: any[] = []
  let lorebooks: any[] = []
  let cantrips: any[] = []

  let historyShow = false
  let historyId = ''
  let historyName = ''

  function openHistory(m: any) {
    historyId = m.id
    historyName = m.name || ''
    historyShow = true
  }

  let editForm: any = {
    name: '', description: '', tag: '', is_public: false, version: '1.0',
    author: '', global_llm_instructions: '', final_verification_enabled: false,
    stages: [],
  }

  async function load() {
    loading = true
    try {
      const [mapData, epData, lbData, cantripData] = await Promise.all([
        api.listMaps(), api.listEndpoints(), api.listLorebooks(), api.listCantrips(),
      ])
      maps = mapData.maps
      endpoints = epData.endpoints
      lorebooks = lbData.lorebooks
      cantrips = cantripData.cantrips
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  function newMap() {
    editForm = {
      name: '', description: '', tag: '', is_public: false, version: '1.0',
      author: '', global_llm_instructions: '', final_verification_enabled: false,
      stages: [createEmptyStage('Writing LLM')],
    }
    editing = true
    selectedMap = null
  }

  function createEmptyStage(name: string) {
    return {
      name,
      description: '',
      system_instructions: '',
      endpoint_id: null,
      endpoint_tag: '',
      model_override: '',
      driver_callable_turns: 0,
      verification_enabled: false,
      verification_endpoint_id: null,
      verification_model: '',
      verification_max_retries: 2,
      verification_instructions: '',
      output_mode: 'persist',
      base_prompt_mode: 'full',
      history_window: 0,
      condition_json: '',
      parameters: [],
      verification_parameters: [],
      resources: [],
    }
  }

  // The condition editor works on a parsed {path, op, value} object and
  // re-serializes into condition_json on every change, so the saved payload
  // is always the JSON the backend validates.
  function condParts(stage: any) {
    if (!stage._cond) {
      let parsed: any = null
      try { parsed = stage.condition_json ? JSON.parse(stage.condition_json) : null } catch { parsed = null }
      stage._cond = parsed && parsed.path
        ? { path: parsed.path, op: parsed.op || 'exists', value: parsed.value ?? '' }
        : { path: '', op: 'exists', value: '' }
    }
    return stage._cond
  }

  function syncCond(stage: any) {
    const c = condParts(stage)
    if (!c.path) { stage.condition_json = ''; return }
    const cond: any = c.op === 'exists' || c.op === 'not_exists'
      ? { path: c.path, op: c.op }
      : { path: c.path, op: c.op, value: c.value }
    stage.condition_json = JSON.stringify(cond)
  }

  // A stage names two models -- one to generate with, one to check with -- so
  // each picker is built from its own endpoint's curated list.
  function modelsFor(endpointId: string | null): string[] {
    const ep = endpoints.find((e: any) => e.id === endpointId)
    return (ep?.models || []).map((m: any) => m.name).filter(Boolean)
  }

  function addStage() {
    const stageNum = editForm.stages.length + 1
    editForm.stages.push(createEmptyStage(`Stage ${stageNum}`))
    editForm = { ...editForm }
  }

  function removeStage(idx: number) {
    editForm.stages.splice(idx, 1)
    editForm = { ...editForm }
  }

  function startEdit(m: any) {
    selectedMap = m
    editing = true
    api.getMap(m.id).then((data: any) => {
      editForm = {
        name: data.name, description: data.description, tag: data.tag,
        is_public: data.is_public, version: data.version, author: data.author,
        global_llm_instructions: data.global_llm_instructions,
        final_verification_enabled: !!data.final_verification_enabled,
        stages: data.stages.map((s: any) => ({
          ...s,
          history_window: Number(s.history_window) || 0,
          resources: s.resources || [],
        })),
      }
    })
  }

  async function saveMap() {
    error = ''
    try {
      // _cond is the condition editor's transient state; the payload carries
      // the serialized condition_json only.
      const payload = {
        ...editForm,
        stages: editForm.stages.map(({ _cond, ...s }: any) => {
          if (!s.base_prompt_mode) s.base_prompt_mode = 'full'
          if (s.condition_json === undefined) s.condition_json = ''
          s.history_window = Number(s.history_window) || 0
          return s
        }),
      }
      if (selectedMap) {
        await api.updateMap(selectedMap.id, payload)
      } else {
        await api.createMap(payload)
      }
      editing = false
      selectedMap = null
      await withScroll(load)
    } catch (e: any) { error = e.message }
  }

  async function deleteMap(id: string) {
    if (!confirm('Delete this map?')) return
    try { await api.deleteMap(id); await withScroll(load) }
    catch (e: any) { error = e.message }
  }

  async function handleDuplicate(m: any) {
    if (duplicatingId) return
    duplicatingId = m.id
    error = ''
    try {
      const copy = await api.duplicateMap(m.id)
      await withScroll(load)
      const fresh = maps.find((item: any) => item.id === copy.id) || copy
      startEdit(fresh)
    } catch (e: any) {
      error = e.message
    } finally {
      duplicatingId = null
    }
  }

  function toggleStageResource(stage: any, type: string, id: string, position: string = 'pre_driver') {
    const existing = stage.resources.findIndex((r: any) => r.resource_type === type && r.resource_id === id && r.position === position)
    if (existing >= 0) {
      stage.resources.splice(existing, 1)
    } else {
      stage.resources.push({ resource_type: type, resource_id: id, position, sticky: false })
    }
    stage.resources = [...stage.resources]
  }

  function isResourceChecked(stage: any, type: string, id: string, position: string = 'pre_driver') {
    return stage.resources.some((r: any) => r.resource_type === type && r.resource_id === id && r.position === position)
  }

  function toggleSticky(stage: any, type: string, id: string) {
    const r = stage.resources.find((r: any) => r.resource_type === type && r.resource_id === id)
    if (r) {
      r.sticky = !r.sticky
      stage.resources = [...stage.resources]
    }
  }

  function isSticky(stage: any, type: string, id: string) {
    return stage.resources.find((r: any) => r.resource_type === type && r.resource_id === id)?.sticky || false
  }

  let showImport = false
  let importJson = ''
  let importName = ''
  let importMode = 'smart'

  async function exportMap(m: any, mode: 'embedded' | 'linked' = 'embedded') {
    try {
      const data = await api.exportMap(m.id, mode)
      downloadJson(data, `${data.name || 'map'}${mode === 'linked' ? '.linked' : ''}.json`)
    } catch (e: any) { error = e.message }
  }

  async function handleImport() {
    error = ''
    try {
      const data = JSON.parse(importJson)
      const result = await withScanOverride((overrideScan: boolean) =>
        api.importMap(data, importName || undefined, importMode, overrideScan),
      )

      const notes: string[] = [...(result.import_warnings || [])]
      if ((result.requires_repos || []).length) {
        notes.push(
          `This map links resources from a repository you have not added: ${result.requires_repos.join(', ')}. ` +
          `Add it under Content Packs and install the map from there to resolve them.`
        )
      }
      if (notes.length) alert(notes.join('\n\n'))
      showImport = false
      importJson = ''
      importName = ''
      importMode = 'smart'
      await withScroll(load)
    } catch (e: any) { error = e.message || 'Invalid JSON' }
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
    const match = maps.find((item: any) => item.id === id)
    if (match) startEdit(match)
  }

  onMount(async () => {
    await load()
    openFromRoute()
  })

  // A link clicked while this page is already open changes only the hash.
  $: if ($routeParams.params.id && maps.length) openFromRoute()
</script>

<div class="page-header">
  <h2>Maps <a class="help-link" href="/help/user-guide.html#maps" target="_blank" title="Open documentation">?</a></h2>
  <div>
    {#if selectedMap || editing}<button onclick={() => { editing = false; selectedMap = null }}>Back to List</button>{/if}
    {#if !editing}
      <button onclick={() => showImport = true}>Import JSON</button>
      <button class="primary" onclick={newMap}>+ Add Map</button>
    {/if}
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

{#if loading}<div class="loading">Loading...</div>
{:else if editing}
  <div class="card">
    <h3>{selectedMap ? 'Edit Map' : 'New Map'}</h3>
    <div class="form-group"><label for="map-name">Name</label><input id="map-name" bind:value={editForm.name} placeholder="Tabletop RPG Map" /></div>
    <div class="form-group"><label for="map-desc">Description</label><input id="map-desc" bind:value={editForm.description} placeholder="Multi-stage writing + gamemaster pipeline" /></div>
    <div class="form-row">
      <div class="form-group"><label for="map-tag">Tag (optional)</label><input id="map-tag" bind:value={editForm.tag} placeholder="tabletop" /></div>
      <div class="form-group"><label for="map-version">Version</label><input id="map-version" bind:value={editForm.version} placeholder="1.0" /></div>
    </div>
    <div class="form-group"><label for="map-author">Author</label><input id="map-author" bind:value={editForm.author} /></div>
    <div class="form-group">
      <label><input type="checkbox" bind:checked={editForm.is_public} style="width: auto;"> Public</label>
    </div>
    <div class="form-group">
      <label><input type="checkbox" bind:checked={editForm.final_verification_enabled} style="width: auto;"> Final-Output Navigator Pass (report-only)</label>
      <p style="color: var(--text-dim); font-size: 11px; margin-top: 2px;">
        Report-only check after all stages finish (respects <code>&lt;VERIFY:off&gt;</code>, never re-runs the map). If a map needs verification with retries, enable verification inside the specific stage instead — the end-of-map pass judges output after every stage is already paid for.
      </p>
    </div>
    <div class="form-group">
      <label for="map-global">Global LLM Instructions (shown to all stages)</label>
      <CodeEditor id="map-global" bind:value={editForm.global_llm_instructions} language="markdown" minHeight="100px" placeholder="Instructions that apply to every LLM in the chain..." />
    </div>
  </div>

  {#each editForm.stages as stage, idx}
    <div class="card">
      <div class="card-header">
        <h3>Stage {idx + 1}: {stage.name}</h3>
        {#if editForm.stages.length > 1}
          <button class="danger" onclick={() => removeStage(idx)}>Remove Stage</button>
        {/if}
      </div>

      <div class="form-group"><label>Stage Name</label><input bind:value={stage.name} /></div>
      <div class="form-group">
        <label>System Instructions</label>
        <CodeEditor bind:value={stage.system_instructions} language="markdown" minHeight="100px" placeholder="Instructions specific to this stage's LLM..." />
      </div>

      <div class="form-row">
        <div class="form-group">
          <label>Endpoint (blank = default)</label>
          <select bind:value={stage.endpoint_id}>
            <option value={null}>Use default endpoint</option>
            {#each endpoints as ep}<option value={ep.id}>{ep.name}</option>{/each}
          </select>
        </div>
        <div class="form-group">
          <label for="stage-model-{idx}">Model Override</label>
          <ModelSelect
            id="stage-model-{idx}"
            bind:value={stage.model_override}
            models={modelsFor(stage.endpoint_id)}
            emptyLabel="Use default"
            placeholder="Leave blank for default"
          />
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <label>Endpoint Tag (takes priority over Endpoint)</label>
          <input bind:value={stage.endpoint_tag} placeholder="e.g. planner — blank to use the Endpoint above" />
          <div style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">Routes this stage to any endpoint carrying this tag, with failover. Lets a shared map name the role a stage needs instead of pinning one endpoint.</div>
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <label>Driver-Callable Turns</label>
          <input type="number" bind:value={stage.driver_callable_turns} min="0" />
        </div>
        <div class="form-group">
          <label>Output Mode</label>
          <select bind:value={stage.output_mode}>
            <option value="persist">Persist (output feeds next stage as context)</option>
            <option value="sanitize">Sanitize (output as system context block)</option>
            <option value="discard">Discard (output not passed forward)</option>
            <option value="capture">Capture (output goes to this stage's cantrips)</option>
          </select>
        </div>
        <div class="form-group">
          <label>Base Prompt</label>
          <select bind:value={stage.base_prompt_mode}>
            <option value="full">Full (client prompt + injections)</option>
            <option value="blank">Blank (stage instructions only)</option>
          </select>
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <label for="stage-history-{idx}">History Window</label>
          <input id="stage-history-{idx}" type="number" min="0" bind:value={stage.history_window} />
          <div style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            0 = full history; N &gt; 0 keeps newest N non-system messages (newest user message always kept)
          </div>
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <label>Run Condition (optional)</label>
          {#if true}
            {@const c = condParts(stage)}
            <div style="display: flex; gap: 8px; align-items: center;">
              <input bind:value={c.path} oninput={() => syncCond(stage)}
                placeholder="chat_data path, e.g. world.day" style="flex: 1;" />
              <select bind:value={c.op} onchange={() => syncCond(stage)}>
                <option value="exists">exists</option>
                <option value="not_exists">is missing</option>
                <option value="==">==</option>
                <option value="!=">!=</option>
                <option value=">=">&ge;</option>
                <option value=">">&gt;</option>
                <option value="<=">&le;</option>
                <option value="<">&lt;</option>
              </select>
              {#if c.op !== 'exists' && c.op !== 'not_exists'}
                <input bind:value={c.value} oninput={() => syncCond(stage)}
                  placeholder="value" style="width: 120px;" />
              {/if}
            </div>
          {/if}
          <div style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            The stage runs only when the chat's stored data satisfies this. Blank = always run. Checked before anything the stage would spend.
          </div>
        </div>
      </div>

      <div class="form-group">
        <label>Attached Lorebooks</label>
        <div style="display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px;">
          {#each lorebooks as lb}
            <label style="display: flex; align-items: center; gap: 4px; font-size: 12px;">
              <input type="checkbox" style="width: auto;"
                checked={isResourceChecked(stage, 'lorebook', lb.id)}
                onchange={() => toggleStageResource(stage, 'lorebook', lb.id)}
              /> {lb.name}
            </label>
          {/each}
        </div>
      </div>

      <div class="form-group">
        <label>Attached Cantrips (Pre-Driver)</label>
        <div style="display: flex; flex-direction: column; gap: 4px; margin-top: 4px;">
          {#each cantrips as c}
            <label style="display: flex; align-items: center; gap: 8px; font-size: 12px;">
              <input type="checkbox" style="width: auto;"
                checked={isResourceChecked(stage, 'cantrip', c.id)}
                onchange={() => toggleStageResource(stage, 'cantrip', c.id)}
              /> {c.name}
              {#if isResourceChecked(stage, 'cantrip', c.id)}
                <label style="margin-left: 12px; display: flex; align-items: center; gap: 2px;">
                  <input type="checkbox" style="width: auto;"
                    checked={isSticky(stage, 'cantrip', c.id)}
                    onchange={() => toggleSticky(stage, 'cantrip', c.id)}
                  /> Sticky
                </label>
              {/if}
            </label>
          {/each}
        </div>
      </div>

      <div class="form-group">
        <label><input type="checkbox" bind:checked={stage.verification_enabled} style="width: auto;"> Enable Verification for this stage</label>
      </div>

      {#if stage.verification_enabled}
        <div style="margin-left: 16px; padding-left: 16px; border-left: 2px solid var(--border);">
          <div class="form-group">
            <label>Verification Instructions</label>
            <CodeEditor bind:value={stage.verification_instructions} language="markdown" minHeight="80px" placeholder="The response must..." />
          </div>
          <div class="form-row">
            <div class="form-group">
              <label>Verification Endpoint</label>
              <select bind:value={stage.verification_endpoint_id}>
                <option value={null}>Use default verification endpoint</option>
                {#each endpoints as ep}<option value={ep.id}>{ep.name}</option>{/each}
              </select>
            </div>
            <div class="form-group">
              <label for="stage-vmodel-{idx}">Verification Model</label>
              <ModelSelect
                id="stage-vmodel-{idx}"
                bind:value={stage.verification_model}
                models={modelsFor(stage.verification_endpoint_id)}
                emptyLabel="Use default"
                placeholder="Leave blank for default"
              />
            </div>
          </div>
          <div class="form-group">
            <label>Max Retries</label>
            <input type="number" bind:value={stage.verification_max_retries} min="0" />
          </div>
          <ParameterEditor
            bind:params={stage.verification_parameters}
            title="Parameters for this stage's verification call"
            scopeNote="Inherits the verification endpoint's parameters."
          />
        </div>
      {/if}

      <ParameterEditor
        bind:params={stage.parameters}
        title="Parameters for this stage"
        hint="Applied to this stage's own LLM call. Overrides the endpoint's and the model's."
        scopeNote="This stage inherits the endpoint's parameters."
      />
    </div>
  {/each}

  {#if editForm.stages.length < 10}
    <div style="margin-bottom: 16px;">
      <button onclick={addStage}>+ Add Stage</button>
    </div>
  {/if}

  <div style="display: flex; gap: 8px; margin-bottom: 24px;">
    <button class="primary" onclick={saveMap}>{selectedMap ? 'Update Map' : 'Create Map'}</button>
    <button onclick={() => { editing = false; selectedMap = null }}>Cancel</button>
  </div>

{:else if maps.length === 0}
  <div class="card">
    <h3>What are Maps?</h3>
    <p style="color: var(--text-dim); font-size: 13px; line-height: 1.8;">
      Maps are all-inclusive workflow presets that chain multiple LLM stages together.
      Each stage can have its own lorebooks, cantrips, verification, and LLM endpoint.
      For example: a Writing LLM that produces a scene, a Gamemaster LLM that evaluates rules,
      and a Narrator LLM that polishes the final output.
      <br><br>
      Maps can be activated via <code style="color: var(--accent);">&lt;#map-tag#&gt;</code> tags,
      exported/imported as JSON, and shared via content packs.
    </p>
  </div>
{:else}
  <table>
    <thead><tr><th>Name</th><th>Stages</th><th>Tag</th><th>Active</th><th>Actions</th></tr></thead>
    <tbody>
      {#each maps as m}
        <tr data-scroll-anchor={m.id}>
          <td>
            <strong>{m.name}</strong>
            {#if m.description}<div style="font-size: 11px; color: var(--text-dim);">{m.description}</div>{/if}
          </td>
          <td>{m.stage_count}</td>
          <td>
            {#if m.tag}
              <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; cursor: pointer;"
                    onclick={() => navigator.clipboard.writeText('<#map-' + m.tag + '#>')}>{m.tag}</span>
            {/if}
          </td>
          <td>
            <button onclick={() => { api.updateMap(m.id, { is_active: !m.is_active }).then(() => withScroll(load)) }}
                   style="font-size: 11px;" class={m.is_active ? 'primary' : ''}>{m.is_active ? 'ON' : 'OFF'}</button>
          </td>
          <td>
            <button onclick={() => startEdit(m)}>Edit</button>
            <button onclick={() => handleDuplicate(m)} disabled={duplicatingId !== null}>
              {duplicatingId === m.id ? 'Duplicating…' : 'Duplicate'}
            </button>
            <button onclick={() => openHistory(m)}>History</button>
            <button onclick={() => exportMap(m)} title="Full content of every attached resource — installs anywhere">Export</button>
            <button onclick={() => exportMap(m, 'linked')} title="References instead of copies where the origin is known — the map tracks upstream">Export (linked)</button>
            <button class="danger" onclick={() => deleteMap(m.id)}>Delete</button>
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

<SnapshotHistoryModal
  bind:show={historyShow}
  resourceType="map"
  resourceId={historyId}
  resourceName={historyName}
  onRestored={() => withScroll(load)}
/>

{#if showImport}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showImport = false; }}>
    <div class="modal" style="width: 700px;">
      <h3>Import Map JSON</h3>
      {#if error}<div class="error-msg">{error}</div>{/if}
      <div class="form-group">
        <label for="import-name">Map Name (optional — defaults to name in JSON)</label>
        <input id="import-name" bind:value={importName} placeholder="Imported Map" />
      </div>
      <div class="form-group">
        <label for="import-mode">Resource Handling</label>
        <select id="import-mode" bind:value={importMode}>
          <option value="smart">Smart (reuse resources you already have)</option>
          <option value="keep_both">Keep Both (always create new copies)</option>
          <option value="reuse">Reuse Existing (link to same-named resources)</option>
          <option value="overwrite">Overwrite (update same-named resources)</option>
        </select>
      </div>
      <div class="form-group">
        <label for="import-file">Load from File</label>
        <input id="import-file" type="file" accept=".json" onchange={handleFileLoad} />
      </div>
      <div class="form-group">
        <label for="import-json">Or paste JSON</label>
        <textarea id="import-json" bind:value={importJson} placeholder="Paste map JSON here..." style="min-height: 200px; font-family: monospace; font-size: 11px;"></textarea>
      </div>
      <div class="modal-actions">
        <button onclick={() => showImport = false}>Cancel</button>
        <button class="primary" onclick={handleImport}>Import</button>
      </div>
    </div>
  </div>
{/if}
