<script lang="ts">
  import { api, getToken } from '../api'
  import { onMount } from 'svelte'
  import { isAdmin } from '../stores'

  let repos: any[] = []
  let installed: any[] = []
  let browseData: any = null
  let loading = true
  let error = ''
  let disclaimer = ''

  let showLinkForm = false
  let linkForm = { name: '', url: '', branch: 'main', token: '' }
  let linking = false

  let showLocalForm = false
  let localForm = { name: '', path: '', is_global: true }
  let localLinking = false

  let packTab = 'browse'
  let allResources: any[] = []
  let selectedResources: Record<string, boolean> = {}
  let packMeta = { pack_name: '', pack_author: '', pack_description: '' }
  let creatingPack = false

  function autofillName() {
    if (linkForm.name.trim()) return
    const url = linkForm.url.trim()
    if (!url) return
    try {
      const u = new URL(url)
      const parts = u.pathname.split('/').filter(Boolean)
      if (parts.length >= 2) {
        linkForm.name = `${parts[0]}/${parts[1].replace(/\.git$/, '')}`
      }
    } catch {}
  }

  let filterRepo = ''
  let filterType = ''
  let filterAuthor = ''
  let sortBy = 'name'

  let browseRepoId = ''
  let installingPath = ''

  async function load() {
    loading = true
    try {
      const [r, i] = await Promise.all([api.listRepos(), api.listInstalled()])
      repos = r.repos
      installed = i.items
      disclaimer = r.disclaimer
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  async function linkRepo() {
    linking = true; error = ''
    try {
      browseData = await api.linkRepo(linkForm)
      showLinkForm = false
      linkForm = { name: '', url: '', branch: 'main', token: '' }
      await load()
    } catch (e: any) { error = e.message }
    finally { linking = false }
  }

  async function browse(id: string) {
    try {
      browseData = await api.browseRepo(id)
      browseRepoId = id
      filterRepo = id
    } catch (e: any) { error = e.message }
  }

  async function sync(id: string) {
    try {
      browseData = await api.syncRepo(id)
      browseRepoId = id
      await load()
    } catch (e: any) { error = e.message }
  }

  async function checkUpdates(id: string) {
    error = ''
    try {
      const result = await api.checkUpdates(id)
      if (result.updates_available > 0) {
        alert(`Updates available for ${result.updates_available} of ${result.checked} installed item(s).\n\nSync the repo and reinstall to update.`)
      } else if (result.checked > 0) {
        alert(`No updates found. Checked ${result.checked} item(s).`)
      } else {
        alert('No installed items from this repo to check.')
      }
      await load()
    } catch (e: any) { error = e.message }
  }

  async function install(repoId: string, filePath: string, fork: boolean) {
    error = ''
    installingPath = filePath
    try {
      const result = await api.installFile({ repo_id: repoId, file_path: filePath, fork })
      if (result.scan?.max_severity === 'critical') {
        alert(`WARNING: ${result.scan.findings.map((f: any) => f.description).join(', ')}\n\nInstalled in disabled state.`)
      } else if (result.scan?.max_severity === 'warning') {
        alert(`Scan warnings: ${result.scan.findings.map((f: any) => f.description).join(', ')}\n\nInstalled in disabled state.`)
      }
      await load()
    } catch (e: any) { error = e.message }
    finally { installingPath = '' }
  }

  async function toggleItem(id: string) {
    try { await api.toggleInstalled(id); await load() }
    catch (e: any) { error = e.message }
  }

  async function uninstall(id: string) {
    if (!confirm('Uninstall this item? The local resource will be deleted.')) return
    try { await api.uninstallItem(id); await load() }
    catch (e: any) { error = e.message }
  }

  async function deleteRepo(id: string) {
    if (!confirm('Remove this repository link? Installed items from this repo will remain.')) return
    try { await api.deleteRepo(id); browseData = null; await load() }
    catch (e: any) { error = e.message }
  }

  async function linkLocalRepo() {
    localLinking = true; error = ''
    try {
      browseData = await api.linkLocalRepo(localForm)
      showLocalForm = false
      localForm = { name: '', path: '', is_global: true }
      await load()
    } catch (e: any) { error = e.message }
    finally { localLinking = false }
  }

  async function loadAllResources() {
    try {
      const [cantrips, lorebooks, skills, scenarioRules, verifyRules, maps] = await Promise.all([
        api.listCantrips(), api.listLorebooks(), api.listSkills(),
        api.listScenarioRules(), api.listVerificationRules(), api.listMaps(),
      ])
      allResources = [
        ...cantrips.cantrips.map((c: any) => ({ id: c.id, type: 'cantrip', name: c.name, description: c.description || '' })),
        ...lorebooks.lorebooks.map((l: any) => ({ id: l.id, type: 'lorebook', name: l.name, description: l.description || '' })),
        ...skills.skills.map((s: any) => ({ id: s.id, type: 'skill', name: s.name, description: s.description || '' })),
        ...scenarioRules.rules.map((r: any) => ({ id: r.id, type: 'scenario_rule', name: r.name, description: '' })),
        ...verifyRules.rules.map((r: any) => ({ id: r.id, type: 'rule', name: r.name, description: r.description || '' })),
        ...maps.maps.map((m: any) => ({ id: m.id, type: 'map', name: m.name, description: m.description || '' })),
      ]
    } catch (e: any) { error = e.message }
  }

  async function handleCreatePack() {
    error = ''
    const resources = Object.entries(selectedResources)
      .filter(([_, checked]) => checked)
      .map(([key, _]) => {
        const [type, id] = key.split(':')
        return { type, id }
      })
    if (resources.length === 0) {
      error = 'Select at least one resource'
      return
    }
    creatingPack = true
    try {
      const blob = await api.createPack({ ...packMeta, resources })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${(packMeta.pack_name || 'content-pack').toLowerCase().replace(/\s+/g, '-')}.zip`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e: any) { error = e.message }
    finally { creatingPack = false }
  }

  let editingResource: any = null
  let editName = ''
  let editDescription = ''
  let savingResource = false

  function startEditResource(r: any) {
    editingResource = r
    editName = r.name
    editDescription = r.description || ''
  }

  async function saveResourceEdit() {
    if (!editingResource) return
    savingResource = true
    error = ''
    try {
      const { type, id } = editingResource
      const updates: any = { name: editName }
      if (type !== 'scenario_rule') {
        updates.description = editDescription
      }
      if (type === 'cantrip') await api.updateCantrip(id, updates)
      else if (type === 'lorebook') await api.updateLorebook(id, updates)
      else if (type === 'skill') await api.updateSkill(id, updates)
      else if (type === 'scenario_rule') await api.updateScenarioRule(id, updates)
      else if (type === 'rule') await api.updateVerificationRule(id, updates)
      else if (type === 'map') await api.updateMap(id, updates)
      editingResource = null
      await loadAllResources()
    } catch (e: any) { error = e.message }
    finally { savingResource = false }
  }

  let previewingResource: any = null
  let previewData: any = null
  let previewLoading = false

    async function previewResource(r: any) {
    previewingResource = r
    previewData = null
    previewLoading = true
    try {
      const { type, id } = r
      if (type === 'cantrip') previewData = await api.getCantrip(id)
      else if (type === 'lorebook') previewData = await api.getLorebook(id)
      else if (type === 'skill') previewData = await api.getSkill(id)
      else if (type === 'scenario_rule') previewData = await api.getScenarioRule(id)
      else if (type === 'rule') previewData = await api.getVerificationRule(id)
      else if (type === 'map') previewData = await api.getMap(id)
    } catch (e: any) { error = e.message }
    finally { previewLoading = false }
  }

  const typeRoutes: Record<string, string> = {
    cantrip: '/cantrips',
    lorebook: '/lorebooks',
    skill: '/skills',
    scenario_rule: '/memories',
    rule: '/verification',
    map: '/maps',
  }

  function gotoResource(r: any) {
    const route = typeRoutes[r.type]
    if (route) {
      window.location.hash = `${route}?id=${r.id}`
    }
  }

  $: filteredResources = packTab === 'create'
    ? allResources.filter(f => !filterType || f.type === filterType)
    : []
  $: allFiles = browseData?.files || []
  $: authors = [...new Set(allFiles.map((f: any) => f.author).filter(Boolean))].sort()
  $: filteredFiles = allFiles
    .filter((f: any) => !filterType || f.type === filterType)
    .filter((f: any) => !filterAuthor || f.author === filterAuthor)
    .sort((a: any, b: any) => {
      if (sortBy === 'name') return a.name.localeCompare(b.name)
      if (sortBy === 'updated') return (b.updated || '').localeCompare(a.updated || '')
      if (sortBy === 'type') return a.type.localeCompare(b.type)
      return 0
    })

  function isInstalled(filePath: string): boolean {
    return installed.some(i => i.file_path === filePath)
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Content Packs <a class="help-link" href="/help/user-guide.html#content-packs" target="_blank" title="Open documentation">?</a></h2>
  <div>
    <button onclick={() => packTab = 'browse'} class={packTab === 'browse' ? 'primary' : ''}>Browse</button>
    <button onclick={() => { packTab = 'create'; loadAllResources(); }} class={packTab === 'create' ? 'primary' : ''} style="margin-left: 8px;">Create Pack</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

{#if packTab === 'create'}
  <div class="card">
    <h3>Create Content Pack</h3>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Select resources to export as a git-ready content pack. Includes <code>descriptions.json</code> and <code>README.md</code> with deployment instructions.
    </p>
    <div class="form-row" style="margin-bottom: 12px;">
      <div class="form-group">
        <label for="pk-name">Pack Name</label>
        <input id="pk-name" bind:value={packMeta.pack_name} placeholder="My Awesome Pack" />
      </div>
      <div class="form-group">
        <label for="pk-author">Author</label>
        <input id="pk-author" bind:value={packMeta.pack_author} placeholder="Your name" />
      </div>
    </div>
    <div class="form-group" style="margin-bottom: 16px;">
      <label for="pk-desc">Description</label>
      <input id="pk-desc" bind:value={packMeta.pack_description} placeholder="What's in this pack?" />
    </div>
    <div style="display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center;">
      <select bind:value={filterType} style="width: auto;">
        <option value="">All Types</option>
        <option value="cantrip">Cantrips</option>
        <option value="lorebook">Lorebooks</option>
        <option value="skill">Skills</option>
        <option value="scenario_rule">Scenario Rules</option>
        <option value="rule">Verification Rules</option>
        <option value="map">Maps</option>
      </select>
      <button type="button" onclick={() => { for (const r of filteredResources) selectedResources[`${r.type}:${r.id}`] = true }}>Select All</button>
      <button type="button" onclick={() => { selectedResources = {} }}>Clear</button>
      <span style="font-size: 11px; color: var(--text-dim);">
        {Object.values(selectedResources).filter(Boolean).length} selected
      </span>
    </div>
    {#if filteredResources.length === 0}
      <div class="empty-state">No resources found.</div>
    {:else}
      <table>
        <thead><tr><th style="width: 30px;"></th><th>Name</th><th>Type</th><th>Description</th><th style="width: 100px;">Actions</th></tr></thead>
        <tbody>
          {#each filteredResources as r}
            <tr>
              <td><input type="checkbox" bind:checked={selectedResources[`${r.type}:${r.id}`]} style="width: auto;" /></td>
              <td><strong>{r.name}</strong></td>
              <td style="font-size: 11px;">{r.type}</td>
              <td style="font-size: 11px; color: var(--text-dim);">{r.description || '—'}</td>
              <td style="white-space: nowrap;">
                <button title="Edit name/description" onclick={() => startEditResource(r)} style="font-size: 11px; padding: 2px 6px;">✎</button>
                <button title="Preview" onclick={() => previewResource(r)} style="font-size: 11px; padding: 2px 6px;">👁</button>
                <button title="Open in editor" onclick={() => gotoResource(r)} style="font-size: 11px; padding: 2px 6px;">↗</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
    <div style="margin-top: 16px;">
      <button class="primary" onclick={handleCreatePack} disabled={creatingPack}>
        {creatingPack ? 'Creating...' : 'Export Pack'}
      </button>
    </div>
  </div>
{/if}

{#if packTab === 'browse'}
  <div style="margin: 12px 0;">
    <button class="primary" onclick={() => showLinkForm = true}>+ Link Repository</button>
    {#if $isAdmin}
      <button onclick={() => showLocalForm = true} style="margin-left: 8px;">+ Link Local Folder</button>
    {/if}
  </div>

  <div class="error-msg" style="font-size: 11px;">
    {disclaimer || 'Content from external repositories is not verified. Download at your own risk.'}
  </div>

  {#if loading}<div class="loading">Loading...</div>
  {:else}
  {#if repos.length > 0}
    <div class="card">
      <h3>Linked Repositories</h3>
      <table>
        <thead><tr><th>Name</th><th>URL</th><th>Branch</th><th>Files</th><th>Last Synced</th><th>Actions</th></tr></thead>
        <tbody>
          {#each repos as r}
            <tr>
              <td>
                {r.name}
                {#if r.is_global}<span class="badge approved" style="font-size: 9px; margin-left: 4px;">Global</span>{/if}
                {#if r.is_local}<span class="badge active" style="font-size: 9px; margin-left: 4px;">Local</span>{/if}
              </td>
              <td style="font-size: 11px; color: var(--text-dim);">{r.is_local ? r.url : r.url}</td>
              <td style="font-size: 11px;">{r.branch}</td>
              <td>{r.file_count}</td>
              <td style="font-size: 11px; color: var(--text-dim);">{r.last_synced ? new Date(r.last_synced).toLocaleString() : 'Never'}</td>
              <td>
                <button onclick={() => browse(r.id)} style="font-size: 12px;">Browse</button>
                <button onclick={() => sync(r.id)} style="font-size: 12px;">Sync</button>
                <button onclick={() => checkUpdates(r.id)} style="font-size: 12px;">Updates</button>
                {#if r.can_remove}
                  <button class="danger" onclick={() => deleteRepo(r.id)} style="font-size: 12px;">Remove</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  {#if browseData}
    <div class="card">
      <h3>{browseData.pack_name}</h3>
      {#if browseData.pack_description}
        <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">{browseData.pack_description}</p>
      {/if}
      {#if !browseData.has_manifest}
        <div class="error-msg" style="font-size: 11px; margin-bottom: 12px;">
          No descriptions.json found. Files auto-discovered from folder structure.
        </div>
      {/if}
      {#if browseData.readme}
        <details style="margin-bottom: 16px;">
          <summary style="cursor: pointer; font-size: 13px; color: var(--accent);">Readme</summary>
          <div style="margin-top: 8px; padding: 12px; background: var(--bg); border: 1px solid var(--border); border-radius: 4px; font-size: 12px; line-height: 1.6; white-space: pre-wrap; max-height: 400px; overflow-y: auto;">{browseData.readme}</div>
        </details>
      {/if}

      <div style="display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap;">
        <select bind:value={filterType} style="width: auto;">
          <option value="">All Types</option>
          <option value="cantrip">Cantrips</option>
          <option value="lorebook">Lorebooks</option>
          <option value="rule">Rules</option>
          <option value="scenario_rule">Scenario Rules</option>
          <option value="map">Maps</option>
        </select>
        <select bind:value={filterAuthor} style="width: auto;">
          <option value="">All Authors</option>
          {#each authors as a}<option value={a}>{a}</option>{/each}
        </select>
        <select bind:value={sortBy} style="width: auto;">
          <option value="name">Sort: Name</option>
          <option value="updated">Sort: Updated</option>
          <option value="type">Sort: Type</option>
        </select>
      </div>

      {#if filteredFiles.length === 0}
        <div class="empty-state">No files found matching filters.</div>
      {:else}
        <table>
          <thead><tr><th>Name</th><th>Type</th><th>Author</th><th>Version</th><th>Description</th><th>Actions</th></tr></thead>
          <tbody>
            {#each filteredFiles as f}
              <tr>
                <td><strong>{f.name}</strong></td>
                <td style="font-size: 11px;">{f.type}</td>
                <td style="font-size: 11px;">{f.author || '—'}</td>
                <td style="font-size: 11px;">{f.version || '—'}</td>
                <td style="font-size: 11px; max-width: 300px;">{f.description || '—'}</td>
                <td>
                  {#if isInstalled(f.path)}
                    <span style="font-size: 11px; color: var(--success);">Installed</span>
                  {:else if installingPath === f.path}
                    <button class="primary" disabled style="font-size: 12px;">Installing...</button>
                  {:else}
                    <button class="primary" onclick={() => install(browseRepoId, f.path, false)} style="font-size: 12px;">Install</button>
                    <button onclick={() => install(browseRepoId, f.path, true)} style="font-size: 12px;">Fork</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  {/if}

  {#if installed.length > 0}
    <div class="card">
      <h3>Installed Items</h3>
      <table>
        <thead><tr><th>Name</th><th>Type</th><th>Author</th><th>Version</th><th>Scan</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          {#each installed as i}
            <tr>
              <td><strong>{i.name}</strong>{#if i.update_available} <span style="color: var(--accent); font-size: 10px;">Update!</span>{/if}</td>
              <td style="font-size: 11px;">{i.type}</td>
              <td style="font-size: 11px;">{i.author || '—'}</td>
              <td style="font-size: 11px;">{i.installed_version}</td>
              <td style="font-size: 11px;">
                {#if i.scan_result}
                  {@const scan = JSON.parse(i.scan_result)}
                  {#if scan.max_severity === 'clean'}
                    <span style="color: var(--success);">Clean</span>
                  {:else if scan.max_severity === 'critical'}
                    <span style="color: var(--danger);">Critical</span>
                  {:else if scan.max_severity === 'warning'}
                    <span style="color: var(--warning, orange);">Warning</span>
                  {/if}
                {/if}
              </td>
              <td>
                {#if i.is_enabled}
                  <span style="color: var(--success); font-size: 11px;">Enabled</span>
                {:else}
                  <span style="color: var(--text-dim); font-size: 11px;">Disabled</span>
                {/if}
              </td>
              <td>
                <button onclick={() => toggleItem(i.id)} style="font-size: 12px;">{i.is_enabled ? 'Disable' : 'Enable'}</button>
                <button class="danger" onclick={() => uninstall(i.id)} style="font-size: 12px;">Uninstall</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if repos.length === 0}
    <div class="empty-state">
      No repositories linked yet. Click "Link Repository" to browse and install content packs from any git repository.
    </div>
  {/if}
{/if}
{/if}

{#if showLinkForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showLinkForm = false; }}>
    <div class="modal">
      <h3>Link Repository</h3>
      <form onsubmit={(e) => { e.preventDefault(); linkRepo(); }}>
        <div class="form-group">
          <label for="lr-name">Name</label>
          <input id="lr-name" bind:value={linkForm.name} placeholder="My Content Pack" required />
        </div>
        <div class="form-group">
          <label for="lr-url">Git URL</label>
          <input id="lr-url" bind:value={linkForm.url} placeholder="https://github.com/user/repo" required onblur={autofillName} />
        </div>
        <div class="form-row">
          <div class="form-group">
            <label for="lr-branch">Branch</label>
            <input id="lr-branch" bind:value={linkForm.branch} placeholder="main" />
          </div>
          <div class="form-group">
            <label for="lr-token">Token (optional)</label>
            <input id="lr-token" type="password" bind:value={linkForm.token} placeholder="Access token for private repos" />
          </div>
        </div>
        <div class="modal-actions">
          <button onclick={() => showLinkForm = false}>Cancel</button>
          <button type="submit" class="primary" disabled={linking}>{linking ? 'Linking...' : 'Link'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

{#if showLocalForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showLocalForm = false; }}>
    <div class="modal">
      <h3>Link Local Folder</h3>
      <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
        Link a folder on this server as a content pack source. All users will be able to browse and install from it.
      </p>
      <form onsubmit={(e) => { e.preventDefault(); linkLocalRepo(); }}>
        <div class="form-group">
          <label for="lf-name">Display Name</label>
          <input id="lf-name" bind:value={localForm.name} placeholder="Admin Content Pack" required />
        </div>
        <div class="form-group">
          <label for="lf-path">Folder Path</label>
          <input id="lf-path" bind:value={localForm.path} placeholder="/data/my-pack or C:\content\my-pack" required />
        </div>
        <div class="form-group">
          <label><input type="checkbox" bind:checked={localForm.is_global} style="width: auto;"> Make visible to all users (Global)</label>
        </div>
        <div class="modal-actions">
          <button onclick={() => showLocalForm = false}>Cancel</button>
          <button type="submit" class="primary" disabled={localLinking}>{localLinking ? 'Linking...' : 'Link'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

{#if editingResource}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) editingResource = null; }}>
    <div class="modal">
      <h3>Edit {editingResource.type}: {editingResource.name}</h3>
      <form onsubmit={(e) => { e.preventDefault(); saveResourceEdit(); }}>
        <div class="form-group">
          <label for="er-name">Name</label>
          <input id="er-name" bind:value={editName} required />
        </div>
        {#if editingResource.type !== 'scenario_rule'}
          <div class="form-group">
            <label for="er-desc">Description</label>
            <input id="er-desc" bind:value={editDescription} placeholder="Short description" />
          </div>
        {/if}
        <div class="modal-actions">
          <button onclick={() => editingResource = null}>Cancel</button>
          <button type="submit" class="primary" disabled={savingResource}>{savingResource ? 'Saving...' : 'Save'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

{#if previewingResource}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) previewingResource = null; }}>
    <div class="modal" style="width: 700px;">
      <h3>Preview: {previewingResource.name}</h3>
      {#if previewLoading}
        <div class="loading">Loading...</div>
      {:else if previewData}
        <div style="background: var(--bg-elevated); border-radius: 8px; padding: 12px; max-height: 500px; overflow-y: auto; font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace; font-size: 11px; line-height: 1.6; white-space: pre-wrap; word-break: break-all;">
          {JSON.stringify(previewData, null, 2)}
        </div>
      {:else}
        <div class="empty-state">Could not load preview.</div>
      {/if}
      <div class="modal-actions">
        <button onclick={() => previewingResource = null}>Close</button>
      </div>
    </div>
  </div>
{/if}
