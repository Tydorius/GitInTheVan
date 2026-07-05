<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'
  import TagEditModal from '../lib/TagEditModal.svelte'

  type Tab = 'groups' | 'tags'
  let tab: Tab = 'groups'

  // Groups state
  let groups: any[] = []
  let lorebooks: any[] = []
  let cantrips: any[] = []
  let loading = true
  let error = ''
  let saved = false

  // Modal state
  let showForm = false
  let editingId: string | null = null
  let form = {
    name: '',
    tag: '',
    is_active: false,
    members: [] as { member_type: string; member_id: string; checked: boolean }[],
  }

  // Tag edit modal
  let tagModalResource: any = null
  let tagModalType: 'lore' | 'cantrip' = 'lore'
  let tagModalName = ''
  let tagModalValue = ''
  let tagModalError = ''

  async function load() {
    loading = true
    try {
      const [grps, lbs, cts] = await Promise.all([
        api.listTagGroups(),
        api.listLorebooks(),
        api.listCantrips(),
      ])
      groups = grps.groups
      lorebooks = lbs.lorebooks
      cantrips = cts.cantrips
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  function openCreate() {
    editingId = null
    form = {
      name: '',
      tag: '',
      is_active: false,
      members: [
        ...lorebooks.map((lb: any) => ({ member_type: 'lorebook', member_id: lb.id, checked: false })),
        ...cantrips.map((c: any) => ({ member_type: 'cantrip', member_id: c.id, checked: false })),
      ],
    }
    showForm = true
  }

  async function openEdit(g: any) {
    editingId = g.id
    const detail = await api.getTagGroup(g.id)
    const memberSet = new Set(detail.members.map((m: any) => `${m.member_type}:${m.member_id}`))
    form = {
      name: detail.name,
      tag: detail.tag,
      is_active: detail.is_active,
      members: [
        ...lorebooks.map((lb: any) => ({
          member_type: 'lorebook', member_id: lb.id,
          checked: memberSet.has(`lorebook:${lb.id}`),
        })),
        ...cantrips.map((c: any) => ({
          member_type: 'cantrip', member_id: c.id,
          checked: memberSet.has(`cantrip:${c.id}`),
        })),
      ],
    }
    showForm = true
  }

  async function handleSubmit() {
    error = ''
    if (!form.name.trim()) { error = 'Name is required'; return }
    try {
      const selectedMembers = form.members
        .filter(m => m.checked)
        .map(m => ({ member_type: m.member_type, member_id: m.member_id }))

      if (editingId) {
        await api.updateTagGroup(editingId, { name: form.name, tag: form.tag, is_active: form.is_active })
        await api.updateTagGroupMembers(editingId, selectedMembers)
      } else {
        await api.createTagGroup({
          name: form.name,
          tag: form.tag,
          is_active: form.is_active,
          members: selectedMembers,
        })
      }
      showForm = false
      await load()
      saved = true
      setTimeout(() => saved = false, 2000)
    } catch (e: any) { error = e.message }
  }

  async function toggleActive(g: any) {
    try {
      await api.updateTagGroup(g.id, { is_active: !g.is_active })
      await load()
    } catch (e: any) { error = e.message }
  }

  async function handleDelete(id: string) {
    if (!confirm('Delete this tag group?')) return
    try {
      await api.deleteTagGroup(id)
      await load()
    } catch (e: any) { error = e.message }
  }

  function memberSummary(g: any): string {
    const lbCount = g.members?.filter((m: any) => m.member_type === 'lorebook').length || 0
    const ctCount = g.members?.filter((m: any) => m.member_type === 'cantrip').length || 0
    const parts: string[] = []
    if (lbCount) parts.push(`${lbCount} Lorebook${lbCount > 1 ? 's' : ''}`)
    if (ctCount) parts.push(`${ctCount} Cantrip${ctCount > 1 ? 's' : ''}`)
    return parts.join(', ') || 'No members'
  }

  // Tag editing (Tags tab)
  function openTagModal(resource: any, type: 'lore' | 'cantrip') {
    tagModalResource = resource
    tagModalType = type
    tagModalName = resource.name
    tagModalValue = resource.tag || ''
    tagModalError = ''
  }

  async function saveTag(tag: string) {
    if (!tagModalResource) return
    try {
      const endpoint = tagModalType === 'lore' ? 'lorebooks' : 'cantrips'
      const resp = await fetch(`/api/${endpoint}/${tagModalResource.id}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('gitv_token')}`,
        },
        body: JSON.stringify({ tag }),
      })
      if (!resp.ok) {
        const data = await resp.json()
        tagModalError = data.detail || 'Failed to update tag'
        return
      }
      tagModalResource = null
      await load()
    } catch (e: any) { tagModalError = e.message }
  }

  async function togglePublic(resource: any, type: 'lore' | 'cantrip') {
    try {
      const endpoint = type === 'lore' ? 'lorebooks' : 'cantrips'
      await fetch(`/api/${endpoint}/${resource.id}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('gitv_token')}`,
        },
        body: JSON.stringify({ is_public: !resource.is_public }),
      })
      await load()
    } catch (e: any) { error = e.message }
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Tags and Groups <a class="help-link" href="/help/user-guide.html#tags-and-groups" target="_blank" title="Open documentation">?</a></h2>
  <div>
    <button onclick={() => tab = 'groups'} class={tab === 'groups' ? 'primary' : ''}>Groups</button>
    <button onclick={() => tab = 'tags'} class={tab === 'tags' ? 'primary' : ''}>Tags</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if saved}<div class="success-msg">Saved.</div>{/if}

{#if loading}<div class="loading">Loading...</div>
{:else if tab === 'groups'}
  <div class="card">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
      <p style="color: var(--text-dim); font-size: 12px; margin: 0;">
        Create collections of lorebooks and cantrips activated by a single tag. Embed <code>&lt;#groupname#&gt;</code> in your persona or scenario.
      </p>
      <button class="primary" onclick={openCreate}>+ Create Group</button>
    </div>

    {#if groups.length === 0}
      <div class="empty-state">No tag groups created. Click "Create Group" to get started.</div>
    {:else}
      <table>
        <thead><tr><th>Name</th><th>Tag</th><th>Members</th><th>Active</th><th>Actions</th></tr></thead>
        <tbody>
          {#each groups as g}
            <tr>
              <td><strong>{g.name}</strong></td>
              <td>
                {#if g.tag}
                  <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; cursor: pointer;"
                        onclick={() => navigator.clipboard.writeText('<#' + g.tag + '#>') }
                        title="Click to copy">&lt;#{g.tag}#&gt;</span>
                {:else}
                  <span style="color: var(--text-dim); font-size: 11px;">(none)</span>
                {/if}
              </td>
              <td style="font-size: 12px; color: var(--text-dim);">{memberSummary(g)}</td>
              <td>
                <button onclick={() => toggleActive(g)} style="padding: 2px 8px; font-size: 11px;" class={g.is_active ? 'primary' : ''}>
                  {g.is_active ? 'ON' : 'OFF'}
                </button>
              </td>
              <td>
                <button onclick={() => openEdit(g)}>Edit</button>
                <button class="danger" onclick={() => handleDelete(g.id)}>Delete</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
{:else if tab === 'tags'}
  <div class="card">
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Manage tags for all your lorebooks and cantrips in one place. Public tags work for all users. Private tags only work for the owner.
    </p>

    <table>
      <thead><tr><th>Name</th><th>Type</th><th>Tag</th><th>Visibility</th></tr></thead>
      <tbody>
        {#each lorebooks as lb}
          <tr>
            <td>{lb.name}</td>
            <td><span class="badge inactive">Lorebook</span></td>
            <td>
              {#if lb.tag}
                <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; cursor: pointer;"
                      onclick={() => navigator.clipboard.writeText('<#lore-' + lb.tag + '#>') }
                      title="Click to copy">&lt;#lore-{lb.tag}#&gt;</span>
                <button onclick={() => openTagModal(lb, 'lore')} style="padding: 0 4px; font-size: 14px; border: none; background: none; cursor: pointer;" title="Edit tag">🖉</button>
              {:else}
                <button onclick={() => openTagModal(lb, 'lore')} style="padding: 2px 8px; font-size: 11px;">+ Tag</button>
              {/if}
            </td>
            <td>
              <button onclick={() => togglePublic(lb, 'lore')} style="padding: 2px 8px; font-size: 11px;" class={lb.is_public ? 'primary' : ''}>
                {lb.is_public ? 'Public' : 'Private'}
              </button>
            </td>
          </tr>
        {/each}
        {#each cantrips as c}
          <tr>
            <td>{c.name}</td>
            <td><span class="badge inactive">Cantrip</span></td>
            <td>
              {#if c.tag}
                <span class="api-key-display" style="display: inline; font-size: 10px; padding: 2px 6px; cursor: pointer;"
                      onclick={() => navigator.clipboard.writeText('<#cantrip-' + c.tag + '#>') }
                      title="Click to copy">&lt;#cantrip-{c.tag}#&gt;</span>
                <button onclick={() => openTagModal(c, 'cantrip')} style="padding: 0 4px; font-size: 14px; border: none; background: none; cursor: pointer;" title="Edit tag">🖉</button>
              {:else}
                <button onclick={() => openTagModal(c, 'cantrip')} style="padding: 2px 8px; font-size: 11px;">+ Tag</button>
              {/if}
            </td>
            <td>
              <button onclick={() => togglePublic(c, 'cantrip')} style="padding: 2px 8px; font-size: 11px;" class={c.is_public ? 'primary' : ''}>
                {c.is_public ? 'Public' : 'Private'}
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>

    {#if lorebooks.length === 0 && cantrips.length === 0}
      <div class="empty-state">No lorebooks or cantrips found. Create some first to manage their tags.</div>
    {/if}
  </div>
{/if}

<!-- Create/Edit Group Modal -->
{#if showForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showForm = false; }}>
    <div class="modal" style="width: 600px;">
      <h3>{editingId ? 'Edit' : 'Create'} Group</h3>
      <form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
        <div class="form-group">
          <label for="grp-name">Name</label>
          <input id="grp-name" bind:value={form.name} required />
        </div>
        <div class="form-group">
          <label for="grp-tag">Group Tag</label>
          <input id="grp-tag" bind:value={form.tag} placeholder="e.g. castlescene" />
          <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
            Embed <code>&lt;#{form.tag || 'tagname'}#&gt;</code> in your persona or scenario to activate this group.
          </p>
        </div>
        <div class="form-group">
          <label>
            <input type="checkbox" bind:checked={form.is_active} style="width: auto;">
            Active (blanket-applied on every message)
          </label>
        </div>
        <div class="form-group">
          <label>Members</label>
          <div style="max-height: 300px; overflow-y: auto; border: 1px solid var(--border); border-radius: 4px; padding: 8px;">
            {#if lorebooks.length > 0}
              <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 4px;">Lorebooks:</div>
              {#each lorebooks as lb, i}
                <label style="display: flex; align-items: center; gap: 6px; padding: 2px 0; font-size: 12px;">
                  <input type="checkbox" bind:checked={form.members[i].checked} style="width: auto;">
                  {lb.name}
                  {#if lb.tag}<span style="color: var(--text-dim); font-size: 10px;">({lb.tag})</span>{/if}
                </label>
              {/each}
            {/if}
            {#if cantrips.length > 0}
              <div style="font-size: 11px; color: var(--text-dim); margin: 8px 0 4px;">Cantrips:</div>
              {#each cantrips as c, j}
                <label style="display: flex; align-items: center; gap: 6px; padding: 2px 0; font-size: 12px;">
                  <input type="checkbox" bind:checked={form.members[lorebooks.length + j].checked} style="width: auto;">
                  {c.name}
                  {#if c.tag}<span style="color: var(--text-dim); font-size: 10px;">({c.tag})</span>{/if}
                </label>
              {/each}
            {/if}
            {#if lorebooks.length === 0 && cantrips.length === 0}
              <div style="color: var(--text-dim); font-size: 12px;">No lorebooks or cantrips available.</div>
            {/if}
          </div>
        </div>
        <div class="modal-actions">
          <button type="button" onclick={() => showForm = false}>Cancel</button>
          <button type="submit" class="primary">{editingId ? 'Save' : 'Create'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Tag Edit Modal -->
<TagEditModal
  show={!!tagModalResource}
  resourceType={tagModalType}
  resourceName={tagModalName}
  currentTag={tagModalValue}
  errorMsg={tagModalError}
  onSave={saveTag}
/>
