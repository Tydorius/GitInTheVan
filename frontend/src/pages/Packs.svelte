<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let repos: any[] = []
  let installed: any[] = []
  let browseData: any = null
  let loading = true
  let error = ''
  let disclaimer = ''

  let showLinkForm = false
  let linkForm = { name: '', url: '', branch: 'main', token: '' }
  let linking = false

  let filterRepo = ''
  let filterType = ''
  let filterAuthor = ''
  let sortBy = 'name'

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
      filterRepo = id
    } catch (e: any) { error = e.message }
  }

  async function sync(id: string) {
    try {
      browseData = await api.syncRepo(id)
      await load()
    } catch (e: any) { error = e.message }
  }

  async function install(repoId: string, filePath: string, fork: boolean) {
    error = ''
    try {
      const result = await api.installFile({ repo_id: repoId, file_path: filePath, fork })
      if (result.scan?.max_severity === 'critical') {
        alert(`WARNING: ${result.scan.findings.map((f: any) => f.description).join(', ')}\n\nInstalled in disabled state.`)
      } else if (result.scan?.max_severity === 'warning') {
        alert(`Scan warnings: ${result.scan.findings.map((f: any) => f.description).join(', ')}\n\nInstalled in disabled state.`)
      }
      await load()
    } catch (e: any) { error = e.message }
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
  <h2>Content Packs</h2>
  <button class="primary" onclick={() => showLinkForm = true}>+ Link Repository</button>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}

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
              <td>{r.name}</td>
              <td style="font-size: 11px; color: var(--text-dim);">{r.url}</td>
              <td style="font-size: 11px;">{r.branch}</td>
              <td>{r.file_count}</td>
              <td style="font-size: 11px; color: var(--text-dim);">{r.last_synced ? new Date(r.last_synced).toLocaleString() : 'Never'}</td>
              <td>
                <button onclick={() => browse(r.id)} style="font-size: 12px;">Browse</button>
                <button onclick={() => sync(r.id)} style="font-size: 12px;">Sync</button>
                <button class="danger" onclick={() => deleteRepo(r.id)} style="font-size: 12px;">Remove</button>
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

      <div style="display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap;">
        <select bind:value={filterType} style="width: auto;">
          <option value="">All Types</option>
          <option value="cantrip">Cantrips</option>
          <option value="lorebook">Lorebooks</option>
          <option value="rule">Rules</option>
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
                  {:else}
                    <button class="primary" onclick={() => install(browseData ? (repos.find(r => r.name === browseData.pack_name)?.id || filterRepo) : '', f.path, false)} style="font-size: 12px;">Install</button>
                    <button onclick={() => install(browseData ? (repos.find(r => r.name === browseData.pack_name)?.id || filterRepo) : '', f.path, true)} style="font-size: 12px;">Fork</button>
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
              <td><strong>{i.name}</strong></td>
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

{#if showLinkForm}
  <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) showLinkForm = false; }}>
    <div class="modal">
      <h3>Link Repository</h3>
      <form onsubmit={(e) => { e.preventDefault(); linkRepo(); }}>
        <div class="form-group">
          <label>Name</label>
          <input bind:value={linkForm.name} placeholder="My Content Pack" required />
        </div>
        <div class="form-group">
          <label>Git URL</label>
          <input bind:value={linkForm.url} placeholder="https://github.com/user/repo or https://gitea.example.com/user/repo" required />
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>Branch</label>
            <input bind:value={linkForm.branch} placeholder="main" />
          </div>
          <div class="form-group">
            <label>Token (optional)</label>
            <input type="password" bind:value={linkForm.token} placeholder="Access token for private repos" />
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
