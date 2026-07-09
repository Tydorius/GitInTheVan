<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'
  import CodeEditor from '../lib/CodeEditor.svelte'
  import { withScroll } from '../lib/scroll'

  let skills: any[] = []
  let endpoints: any[] = []
  let loading = true
  let error = ''
  let saved = false
  let tab = 'skill'

  let showForm = false
  let editingId: string | null = null
  let form = { name: '', description: '', content: '', type: 'skill' }

  async function load() {
    loading = true
    try {
      const [skillData, epData] = await Promise.all([api.listSkills(), api.listEndpoints()])
      skills = skillData.skills
      endpoints = epData.endpoints
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  function resetForm() {
    form = { name: '', description: '', content: '', type: tab }
    editingId = null
  }

  function startCreate() {
    resetForm()
    form.type = tab
    showForm = true
  }

  function startEdit(s: any) {
    editingId = s.id
    form = { name: s.name, description: s.description || '', content: s.content || '', type: s.type }
    showForm = true
  }

  async function handleSubmit() {
    error = ''
    try {
      if (editingId) {
        await api.updateSkill(editingId, form)
      } else {
        await api.createSkill(form)
      }
      showForm = false
      resetForm()
      saved = true
      setTimeout(() => saved = false, 2000)
      await withScroll(load)
    } catch (e: any) { error = e.message }
  }

  async function handleDelete(s: any) {
    if (!confirm(`Delete "${s.name}"?`)) return
    error = ''
    try {
      await api.deleteSkill(s.id)
      await withScroll(load)
    } catch (e: any) { error = e.message }
  }

  async function toggleEndpoint(skillId: string, endpointId: string, currentlyAttached: boolean) {
    error = ''
    try {
      if (currentlyAttached) {
        await api.detachSkill(skillId, endpointId)
      } else {
        await api.attachSkill(skillId, endpointId)
      }
      await withScroll(load)
    } catch (e: any) { error = e.message }
  }

  function endpointName(id: string): string {
    const ep = endpoints.find(e => e.id === id)
    return ep?.name || id.slice(0, 8)
  }

  $: filteredSkills = skills.filter(s => s.type === tab)

  onMount(() => { load() })
</script>

<div class="page-header">
  <h2>Skills &amp; Samples <a class="help-link" href="/help/user-guide.html#skills" target="_blank" title="Open documentation">?</a></h2>
  <div>
    <button onclick={() => tab = 'skill'} class={tab === 'skill' ? 'primary' : ''}>Skills</button>
    <button onclick={() => tab = 'sample'} class={tab === 'sample' ? 'primary' : ''} style="margin-left: 8px;">Writing Samples</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if saved}<div class="success-msg">Saved.</div>{/if}

{#if loading}<div class="loading">Loading...</div>
{:else}
  <div style="margin-bottom: 12px;">
    <button class="primary" onclick={startCreate}>+ New {tab === 'skill' ? 'Skill' : 'Writing Sample'}</button>
    <button onclick={load} style="margin-left: 8px;">Refresh</button>
  </div>

  {#if filteredSkills.length === 0}
    <div class="empty-state">
      No {tab === 'skill' ? 'skills' : 'writing samples'} yet.
      {#if tab === 'skill'}
        Skills are behavioral instructions injected into the system message (e.g. "Always write in third person").
      {:else}
        Writing samples are style references injected before the last message (e.g. "Match this prose style").
      {/if}
    </div>
  {:else}
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Description</th>
          <th>Attached Endpoints</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each filteredSkills as s}
          <tr data-scroll-anchor={s.id}>
            <td><strong>{s.name}</strong></td>
            <td style="font-size: 12px; color: var(--text-dim); max-width: 300px;">{s.description || '—'}</td>
            <td style="font-size: 12px;">
              {#if s.endpoints && s.endpoints.length > 0}
                {s.endpoints.map(endpointName).join(', ')}
              {:else}
                <span style="color: var(--text-dim);">None</span>
              {/if}
            </td>
            <td style="white-space: nowrap;">
              <button onclick={() => startEdit(s)} style="font-size: 12px;">Edit</button>
              <button class="danger" onclick={() => handleDelete(s)} style="font-size: 12px; margin-left: 4px;">Delete</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
{/if}

{#if showForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showForm = false; }}>
    <div class="modal" style="width: 700px;">
      <h3>{editingId ? 'Edit' : 'New'} {form.type === 'skill' ? 'Skill' : 'Writing Sample'}</h3>
      <form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
        <div class="form-group">
          <label for="skill-name">Name</label>
          <input id="skill-name" bind:value={form.name} placeholder={form.type === 'skill' ? 'Combat Writing Expert' : 'Descriptive Prose Style'} required />
        </div>
        <div class="form-group">
          <label for="skill-desc">Description</label>
          <input id="skill-desc" bind:value={form.description} placeholder="Short summary shown in list" />
        </div>
        <div class="form-group">
          <label for="skill-type">Type</label>
          <select id="skill-type" bind:value={form.type}>
            <option value="skill">Skill (injected into system message)</option>
            <option value="sample">Writing Sample (injected before last message)</option>
          </select>
          <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            {#if form.type === 'skill'}
              Skills are behavioral instructions appended to the system message alongside character definition and lorebooks.
            {:else}
              Writing samples are style references inserted before the last user message so the style is fresh in context.
            {/if}
          </p>
        </div>
        <div class="form-group">
          <label for="skill-content">Content</label>
          <CodeEditor bind:value={form.content} language="markdown" minHeight="200px" placeholder={form.type === 'skill' ? 'You are an expert at writing vivid, immersive combat scenes. Describe actions, injuries, and tactics in detail...' : 'Here is an example of the desired writing style:\n\nThe rain fell in sheets...'} />
        </div>

        {#if editingId && endpoints.length > 0}
          <div class="form-group">
            <label>Attached Endpoints</label>
            <div style="display: flex; flex-direction: column; gap: 4px; margin-top: 4px; max-height: 150px; overflow-y: auto;">
              {#each endpoints as ep}
                <label style="display: flex; align-items: center; gap: 6px; font-size: 13px;">
                  <input
                    type="checkbox"
                    checked={skills.find(s => s.id === editingId)?.endpoints?.includes(ep.id)}
                    onchange={() => toggleEndpoint(editingId!, ep.id, skills.find(s => s.id === editingId)?.endpoints?.includes(ep.id) || false)}
                    style="width: auto;"
                  >
                  {ep.name}
                </label>
              {/each}
            </div>
          </div>
        {/if}

        <div class="modal-actions">
          <button onclick={() => { showForm = false; resetForm(); }}>Cancel</button>
          <button type="submit" class="primary">{editingId ? 'Update' : 'Create'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}
