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
  </div>
{/if}

{#if loading}<div class="loading">Loading...</div>
{:else}
  <table>
    <thead><tr><th>Username</th><th>Admin</th><th>Created</th></tr></thead>
    <tbody>
      {#each users as u}
        <tr>
          <td>{u.username}</td>
          <td>{#if u.is_admin}<span class="badge active">Admin</span>{/if}</td>
          <td style="color: var(--text-dim); font-size: 11px;">{new Date(u.created_at).toLocaleDateString()}</td>
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
