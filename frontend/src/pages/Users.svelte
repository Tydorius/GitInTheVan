<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'

  let users: any[] = []
  let loading = true
  let error = ''
  let showForm = false
  let newUsername = ''
  let newPassword = ''
  let createdKey = ''

  let editingUser: any | null = null
  let editUsername = ''
  let resettingPasswordId: string | null = null
  let resetPasswordValue = ''
  let regeneratingKeyId: string | null = null
  let newApiKey = ''

  async function load() {
    loading = true
    try { const data = await api.listUsers(); users = data.users }
    catch (e: any) { error = e.message }
    finally { loading = false }
  }

  async function createUser() {
    error = ''; createdKey = ''
    try {
      const data = await api.createUser(newUsername, newPassword)
      createdKey = data.api_key
      showForm = false; newUsername = ''; newPassword = ''
      await load()
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
      await load()
    } catch (e: any) { error = e.message }
  }

  async function toggleDisabled(u: any) {
    error = ''
    try {
      await api.updateUser(u.id, { is_disabled: !u.is_disabled })
      await load()
    } catch (e: any) { error = e.message }
  }

  async function handleDelete(u: any) {
    if (!confirm(`Delete user "${u.username}"? This removes ALL their data (endpoints, cantrips, lorebooks, memories, settings, etc). This cannot be undone.`)) return
    error = ''
    try {
      await api.deleteUser(u.id)
      await load()
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
      regeneratingKeyId = u.id
    } catch (e: any) { error = e.message }
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Users</h2>
  <button class="primary" onclick={() => showForm = true}>+ Add User</button>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if createdKey}
  <div class="success-msg">
    User created! API key: <div class="api-key-display">{createdKey}</div>
    <p style="font-size: 11px; margin-top: 4px;">Save this key now. It will not be shown again.</p>
  </div>
{/if}
{#if newApiKey}
  <div class="success-msg">
    New API key: <div class="api-key-display">{newApiKey}</div>
    <p style="font-size: 11px; margin-top: 4px;">Save this key now. The old key no longer works.</p>
    <button onclick={() => { newApiKey = ''; regeneratingKeyId = null }} style="font-size: 12px; margin-top: 4px;">Dismiss</button>
  </div>
{/if}

{#if loading}<div class="loading">Loading...</div>
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
              <button onclick={() => startEdit(u)} style="font-size: 12px;" title="Edit username">Edit</button>
              <button onclick={() => { resettingPasswordId = u.id; resetPasswordValue = '' }} style="font-size: 12px;" title="Reset password">Password</button>
              <button onclick={() => handleRegenerateKey(u)} style="font-size: 12px;" title="Regenerate API key">New Key</button>
              <button onclick={() => toggleDisabled(u)} style="font-size: 12px;" title={u.is_disabled ? 'Enable user' : 'Disable user'}>
                {u.is_disabled ? 'Enable' : 'Disable'}
              </button>
              <button class="danger" onclick={() => handleDelete(u)} style="font-size: 12px;" title="Delete user and all data">Delete</button>
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

{#if showForm}
  <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) showForm = false; }}>
    <div class="modal">
      <h3>Add User</h3>
      <form onsubmit={(e) => { e.preventDefault(); createUser(); }}>
        <div class="form-group"><label>Username</label><input bind:value={newUsername} required /></div>
        <div class="form-group"><label>Password</label><input type="password" bind:value={newPassword} required /></div>
        <div class="modal-actions">
          <button onclick={() => showForm = false}>Cancel</button>
          <button type="submit" class="primary">Create</button>
        </div>
      </form>
    </div>
  </div>
{/if}

{#if editingUser}
  <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) editingUser = null; }}>
    <div class="modal">
      <h3>Edit User</h3>
      <form onsubmit={(e) => { e.preventDefault(); saveEdit(); }}>
        <div class="form-group">
          <label>Username</label>
          <input bind:value={editUsername} required />
        </div>
        <div class="modal-actions">
          <button onclick={() => editingUser = null}>Cancel</button>
          <button type="submit" class="primary">Save</button>
        </div>
      </form>
    </div>
  </div>
{/if}

{#if resettingPasswordId}
  <div class="modal-overlay" onclick={(e) => { if (e.target === e.currentTarget) resettingPasswordId = null; }}>
    <div class="modal">
      <h3>Reset Password</h3>
      <form onsubmit={(e) => { e.preventDefault(); handleResetPassword(); }}>
        <div class="form-group">
          <label>New Password</label>
          <input type="password" bind:value={resetPasswordValue} required />
        </div>
        <div class="modal-actions">
          <button onclick={() => resettingPasswordId = null}>Cancel</button>
          <button type="submit" class="primary">Reset</button>
        </div>
      </form>
    </div>
  </div>
{/if}
