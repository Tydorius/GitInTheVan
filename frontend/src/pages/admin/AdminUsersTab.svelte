<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '../../api'
  import Modal from '../../lib/Modal.svelte'

  let users: any[] = $state([])
  let error = $state('')
  let showUserForm = $state(false)
  let newUsername = $state('')
  let newPassword = $state('')
  let createdKey = $state('')
  let editingUser: any | null = $state(null)
  let editUsername = $state('')
  let resettingPasswordId: string | null = $state(null)
  let resetPasswordValue = $state('')
  let newApiKey = $state('')

  async function loadUsers() {
    try { const data = await api.listUsers(); users = data.users }
    catch (e: any) { error = e.message }
  }

  async function createUser() {
    error = ''; createdKey = ''
    try {
      const data = await api.createUser(newUsername, newPassword)
      createdKey = data.api_key
      showUserForm = false; newUsername = ''; newPassword = ''
      await loadUsers()
    } catch (e: any) { error = e.message }
  }

  function startEdit(u: any) {
    editingUser = u
    editUsername = u.username
  }

  async function saveEdit() {
    if (!editingUser) return
    error = ''
    try {
      await api.updateUser(editingUser.id, { username: editUsername })
      editingUser = null
      await loadUsers()
    } catch (e: any) { error = e.message }
  }

  async function toggleDisabled(u: any) {
    error = ''
    try {
      await api.updateUser(u.id, { is_disabled: !u.is_disabled })
      await loadUsers()
    } catch (e: any) { error = e.message }
  }

  async function handleDeleteUser(u: any) {
    if (!confirm(`Delete user "${u.username}"? This removes ALL their data. This cannot be undone.`)) return
    error = ''
    try {
      await api.deleteUser(u.id)
      await loadUsers()
    } catch (e: any) { error = e.message }
  }

  async function handleResetPassword() {
    if (!resettingPasswordId || !resetPasswordValue) return
    error = ''
    try {
      await api.resetUserPassword(resettingPasswordId, resetPasswordValue)
      resettingPasswordId = null
      resetPasswordValue = ''
    } catch (e: any) { error = e.message }
  }

  async function handleRegenerateKey(u: any) {
    if (!confirm(`Regenerate API key for "${u.username}"? The old key will stop working immediately.`)) return
    error = ''
    try {
      const data = await api.regenerateUserApiKey(u.id)
      newApiKey = data.api_key
    } catch (e: any) { error = e.message }
  }

  onMount(loadUsers)
</script>

{#if error}<div class="error-msg">{error}</div>{/if}

<div style="margin-bottom: 12px;">
  <button class="primary" onclick={() => showUserForm = true}>+ Add User</button>
  <button onclick={loadUsers} style="margin-left: 8px;">Refresh</button>
</div>

{#if createdKey}
  <div class="success-msg">
    User created! API key: <div class="api-key-display" style="cursor: pointer; user-select: all;" onclick={() => navigator.clipboard.writeText(createdKey)}>{createdKey}</div>
    <p style="font-size: 11px; margin-top: 4px;">Save this key now. It will not be shown again.</p>
  </div>
{/if}

{#if newApiKey}
  <div class="success-msg">
    New API key: <div class="api-key-display" style="cursor: pointer; user-select: all;" onclick={() => navigator.clipboard.writeText(newApiKey)}>{newApiKey}</div>
    <p style="font-size: 11px; margin-top: 4px;">Save this key now. The old key no longer works.</p>
    <button onclick={() => { newApiKey = '' }} style="font-size: 12px; margin-top: 4px;">Dismiss</button>
  </div>
{/if}

{#if users.length === 0}
  <div class="empty-state">No users.</div>
{:else}
  <table>
    <thead><tr><th>Username</th><th>Role</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead>
    <tbody>
      {#each users as u}
        <tr>
          <td>{u.username}</td>
          <td>{#if u.is_admin}<span class="badge approved">Admin</span>{:else}User{/if}</td>
          <td>
            {#if u.is_disabled}
              <span class="badge violation">Disabled</span>
            {:else}
              <span style="color: var(--success); font-size: 12px;">Active</span>
            {/if}
          </td>
          <td style="color: var(--text-dim); font-size: 11px;">
            {u.created_at ? new Date(u.created_at).toLocaleString() : '—'}
          </td>
          <td style="white-space: nowrap;">
            {#if !u.is_admin}
              <button onclick={() => startEdit(u)} style="font-size: 12px;">Edit</button>
              <button onclick={() => { resettingPasswordId = u.id; resetPasswordValue = '' }} style="font-size: 12px;">Password</button>
              <button onclick={() => handleRegenerateKey(u)} style="font-size: 12px;">New Key</button>
              <button onclick={() => toggleDisabled(u)} style="font-size: 12px;">
                {u.is_disabled ? 'Enable' : 'Disable'}
              </button>
              <button class="danger" onclick={() => handleDeleteUser(u)} style="font-size: 12px;">Delete</button>
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

<Modal open={showUserForm} title="Add User" onclose={() => showUserForm = false}>
  <form onsubmit={(e) => { e.preventDefault(); createUser(); }}>
    <div class="form-group"><label for="user-name">Username</label><input id="user-name" bind:value={newUsername} required /></div>
    <div class="form-group"><label for="user-pass">Password</label><input id="user-pass" type="password" bind:value={newPassword} required /></div>
    <div class="modal-actions">
      <button type="button" onclick={() => showUserForm = false}>Cancel</button>
      <button type="submit" class="primary">Create</button>
    </div>
  </form>
</Modal>

<Modal open={!!editingUser} title="Edit User" onclose={() => editingUser = null}>
  <form onsubmit={(e) => { e.preventDefault(); saveEdit(); }}>
    <div class="form-group">
      <label for="edit-name">Username</label>
      <input id="edit-name" bind:value={editUsername} required />
    </div>
    <div class="modal-actions">
      <button type="button" onclick={() => editingUser = null}>Cancel</button>
      <button type="submit" class="primary">Save</button>
    </div>
  </form>
</Modal>

<Modal open={!!resettingPasswordId} title="Reset Password" onclose={() => resettingPasswordId = null}>
  <form onsubmit={(e) => { e.preventDefault(); handleResetPassword(); }}>
    <div class="form-group">
      <label for="reset-pass">New Password</label>
      <input id="reset-pass" type="password" bind:value={resetPasswordValue} required />
    </div>
    <div class="modal-actions">
      <button type="button" onclick={() => resettingPasswordId = null}>Cancel</button>
      <button type="submit" class="primary">Reset</button>
    </div>
  </form>
</Modal>
