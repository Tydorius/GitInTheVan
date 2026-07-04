<script lang="ts">
  import { api } from '../api'
  import { onMount, onDestroy } from 'svelte'
  import Debug from './Debug.svelte'

  let tab = 'caps'
  let loading = true
  let error = ''
  let saved = false

  let adminSettings: any = null
  let auditLogs: any[] = []
  let serverLogs: string[] = []
  let logLevelInput = ''
  let capsForm = {
    max_driver_callable_turns: 2,
    max_verification_retries: 3,
    max_map_stages: 3,
    rate_limit_proxy_per_min: 60,
    rate_limit_api_per_min: 120,
    runtime_log_level: '',
  }

  let auditAutoRefresh = false
  let serverLogAutoRefresh = false
  let auditInterval: ReturnType<typeof setInterval> | null = null
  let serverLogInterval: ReturnType<typeof setInterval> | null = null

  let users: any[] = []
  let showUserForm = false
  let newUsername = ''
  let newPassword = ''
  let createdKey = ''
  let editingUser: any | null = null
  let editUsername = ''
  let resettingPasswordId: string | null = null
  let resetPasswordValue = ''
  let regeneratingKeyId: string | null = null
  let newApiKey = ''

  let sslStatus: any = null
  let sslIPs = ''
  let sslGenerating = false

  async function load() {
    loading = true
    try {
      adminSettings = await api.getAdminSettings()
      capsForm = {
        max_driver_callable_turns: adminSettings.max_driver_callable_turns,
        max_verification_retries: adminSettings.max_verification_retries,
        max_map_stages: adminSettings.max_map_stages,
        rate_limit_proxy_per_min: adminSettings.rate_limit_proxy_per_min,
        rate_limit_api_per_min: adminSettings.rate_limit_api_per_min,
        runtime_log_level: adminSettings.runtime_log_level || '',
      }
      logLevelInput = adminSettings.runtime_log_level || ''
    } catch (e: any) { error = e.message }
    finally { loading = false }
  }

  async function loadAudit() {
    try {
      const data = await api.getAdminAuditLogs(100, 0)
      auditLogs = data.logs
    } catch (e: any) { error = e.message }
  }

  async function loadServerLogs() {
    try {
      const data = await api.getServerLogs(200)
      serverLogs = data.lines
    } catch (e: any) { error = e.message }
  }

  async function loadUsers() {
    try { const data = await api.listUsers(); users = data.users }
    catch (e: any) { error = e.message }
  }

  function toggleAuditAutoRefresh() {
    auditAutoRefresh = !auditAutoRefresh
    if (auditAutoRefresh) {
      auditInterval = setInterval(loadAudit, 15000)
    } else if (auditInterval) {
      clearInterval(auditInterval)
      auditInterval = null
    }
  }

  function toggleServerLogAutoRefresh() {
    serverLogAutoRefresh = !serverLogAutoRefresh
    if (serverLogAutoRefresh) {
      serverLogInterval = setInterval(loadServerLogs, 15000)
    } else if (serverLogInterval) {
      clearInterval(serverLogInterval)
      serverLogInterval = null
    }
  }

  async function saveCaps() {
    error = ''; saved = false
    try {
      await api.updateAdminSettings(capsForm)
      saved = true
      setTimeout(() => saved = false, 2000)
      await load()
    } catch (e: any) { error = e.message }
  }

  async function applyLogLevel() {
    error = ''
    try {
      await api.updateAdminSettings({ runtime_log_level: logLevelInput })
      await load()
      await loadServerLogs()
    } catch (e: any) { error = e.message }
  }

  async function clearLogLevel() {
    logLevelInput = ''
    await applyLogLevel()
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
      regeneratingKeyId = u.id
    } catch (e: any) { error = e.message }
  }

  async function loadSSL() {
    try {
      sslStatus = await api.getSSLStatus()
    } catch { sslStatus = null }
  }

  async function generateCert() {
    error = ''
    sslGenerating = true
    try {
      const ips = sslIPs.trim() ? sslIPs.trim().split(',').map(s => s.trim()).filter(Boolean) : undefined
      sslStatus = await api.generateSSLCert(ips)
    } catch (e: any) { error = e.message }
    finally { sslGenerating = false }
  }

  onDestroy(() => {
    if (auditInterval) clearInterval(auditInterval)
    if (serverLogInterval) clearInterval(serverLogInterval)
  })

  onMount(() => {
    load()
    loadAudit()
    loadServerLogs()
    loadUsers()
    loadSSL()
  })
</script>

<div class="page-header">
  <h2>Admin <a class="help-link" href="/help/user-guide.html#admin" target="_blank" title="Open documentation">?</a></h2>
  <div>
    <button onclick={() => tab = 'caps'} class={tab === 'caps' ? 'primary' : ''}>Global Caps</button>
    <button onclick={() => tab = 'users'} class={tab === 'users' ? 'primary' : ''}>Users</button>
    <button onclick={() => tab = 'audit'} class={tab === 'audit' ? 'primary' : ''}>Audit Logs</button>
    <button onclick={() => tab = 'logs'} class={tab === 'logs' ? 'primary' : ''}>Server Logs</button>
    <button onclick={() => tab = 'network'} class={tab === 'network' ? 'primary' : ''}>Network</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if saved}<div class="success-msg">Settings saved.</div>{/if}

{#if loading && tab === 'caps'}<div class="loading">Loading...</div>
{:else if tab === 'caps'}
  <div class="card">
    <h3>Global Limits</h3>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      These caps prevent users from causing internal denial-of-service by setting absurdly high turn or retry counts.
      The effective limit is the lower of the user's setting and the global cap.
    </p>
    <div class="form-row">
      <div class="form-group">
        <label for="cap-turns">Max Driver-Callable Turns</label>
        <input id="cap-turns" type="number" bind:value={capsForm.max_driver_callable_turns} min="0" />
      </div>
      <div class="form-group">
        <label for="cap-retries">Max Verification Retries</label>
        <input id="cap-retries" type="number" bind:value={capsForm.max_verification_retries} min="0" />
      </div>
      <div class="form-group">
        <label for="cap-stages">Max Map Stages</label>
        <input id="cap-stages" type="number" bind:value={capsForm.max_map_stages} min="1" />
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label for="cap-proxy-rl">Rate Limit: Proxy (req/min)</label>
        <input id="cap-proxy-rl" type="number" bind:value={capsForm.rate_limit_proxy_per_min} min="0" />
      </div>
      <div class="form-group">
        <label for="cap-api-rl">Rate Limit: Management API (req/min)</label>
        <input id="cap-api-rl" type="number" bind:value={capsForm.rate_limit_api_per_min} min="0" />
      </div>
    </div>
    <button class="primary" onclick={saveCaps}>Save</button>
  </div>

  <div class="card">
    <h3>Runtime Log Level</h3>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Temporarily change the server log level without restarting. This takes effect immediately.
      Leave blank to use the startup default ({adminSettings?.effective_log_level || 'INFO'}).
    </p>
    <div class="form-row">
      <div class="form-group">
        <label for="log-level">Log Level Override</label>
        <select id="log-level" bind:value={logLevelInput}>
          <option value="">Use startup default</option>
          <option value="DEBUG">DEBUG (most verbose)</option>
          <option value="INFO">INFO</option>
          <option value="WARNING">WARNING</option>
          <option value="ERROR">ERROR (least verbose)</option>
          <option value="CRITICAL">CRITICAL</option>
        </select>
      </div>
      <div class="form-group" style="display: flex; align-items: flex-end; gap: 8px;">
        <button class="primary" onclick={applyLogLevel}>Apply</button>
        {#if logLevelInput}<button onclick={clearLogLevel}>Reset to Default</button>{/if}
      </div>
    </div>
    <p style="color: var(--text-dim); font-size: 11px;">
      Current effective level: <strong>{adminSettings?.effective_log_level || 'INFO'}</strong>
    </p>
  </div>

{:else if tab === 'users'}
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
      <button onclick={() => { newApiKey = ''; regeneratingKeyId = null }} style="font-size: 12px; margin-top: 4px;">Dismiss</button>
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

{:else if tab === 'audit'}
  <div style="margin-bottom: 12px;">
    <button onclick={loadAudit}>Refresh</button>
    <button onclick={toggleAuditAutoRefresh} class={auditAutoRefresh ? 'primary' : ''} style="margin-left: 8px;">
      {auditAutoRefresh ? 'Auto ON (15s)' : 'Auto OFF'}
    </button>
  </div>
  {#if auditLogs.length === 0}
    <div class="empty-state">No audit logs recorded.</div>
  {:else}
    <table>
      <thead><tr><th>Action</th><th>Target</th><th>Details</th><th>Time</th></tr></thead>
      <tbody>
        {#each auditLogs as log}
          <tr>
            <td><strong>{log.action}</strong></td>
            <td style="font-size: 11px;">{log.target_type}{#if log.target_id} ({log.target_id.slice(0, 8)}){/if}</td>
            <td style="font-size: 12px; max-width: 300px; overflow: hidden; text-overflow: ellipsis;">{log.details}</td>
            <td style="color: var(--text-dim); font-size: 11px;">{new Date(log.created_at).toLocaleString()}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

{:else if tab === 'logs'}
  <div style="margin-bottom: 12px;">
    <button onclick={loadServerLogs}>Refresh</button>
    <button onclick={toggleServerLogAutoRefresh} class={serverLogAutoRefresh ? 'primary' : ''} style="margin-left: 8px;">
      {serverLogAutoRefresh ? 'Auto ON (15s)' : 'Auto OFF'}
    </button>
  </div>
  <div class="card" style="background: #1a1d27; padding: 0;">
    <div id="server-log-view" style="
      max-height: 600px; overflow-y: auto; padding: 12px;
      font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
      font-size: 11px; line-height: 1.6; color: #c8c9d3;
      white-space: pre-wrap; word-break: break-all;
    ">{serverLogs.join('\n')}</div>
  </div>
{:else if tab === 'network'}
  <div class="card">
    <h3>HTTPS / LAN Access</h3>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Browsers block HTTP requests from HTTPS sites (like JanitorAI). Enabling HTTPS allows
      other devices on your network to use GitInTheVan as a reverse proxy.
      This uses a self-signed certificate &mdash; you must accept it once in each browser.
    </p>

    {#if sslStatus?.is_active}
      <div class="success-msg" style="margin-bottom: 12px;">
        HTTPS is active. Access from other devices via <code>https://YOUR-IP:{location.port}</code>
      </div>
    {:else if sslStatus?.cert_exists}
      <div style="background: var(--bg-elevated); padding: 12px; border-radius: 8px; margin-bottom: 12px; font-size: 12px;">
        Certificate generated but not yet active. Restart the server to enable HTTPS.
      </div>
    {:else}
      <div style="background: var(--bg-elevated); padding: 12px; border-radius: 8px; margin-bottom: 12px; font-size: 12px;">
        No certificate configured. The server is running over HTTP.
      </div>
    {/if}

    {#if sslStatus?.cert_info}
      <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 16px;">
        <div><strong>Subject:</strong> {sslStatus.cert_info.subject}</div>
        <div><strong>Valid until:</strong> {new Date(sslStatus.cert_info.not_after).toLocaleDateString()}</div>
      </div>
    {/if}

    <div class="form-group">
      <label for="ssl-ips">Include IP addresses in certificate (comma-separated, optional)</label>
      <input id="ssl-ips" bind:value={sslIPs} placeholder="10.0.0.187, 192.168.1.50" />
      <p style="font-size: 11px; color: var(--text-dim); margin-top: 4px;">
        Add your machine's LAN IP(s) so browsers trust the cert for those addresses.
      </p>
    </div>

    <button class="primary" onclick={generateCert} disabled={sslGenerating}>
      {sslGenerating ? 'Generating...' : sslStatus?.cert_exists ? 'Regenerate Certificate' : 'Generate Self-Signed Certificate'}
    </button>

    {#if sslStatus?.cert_exists}
      <div style="margin-top: 16px; padding: 16px; background: var(--bg-elevated); border-radius: 8px; font-size: 13px; border-left: 3px solid var(--accent);">
        <p style="margin: 0 0 10px;"><strong>Setup steps (do this on EVERY device):</strong></p>
        <ol style="margin: 0; padding-left: 20px; line-height: 2;">
          <li><strong>Restart</strong> the GitInTheVan server</li>
          <li>Open <code>https://YOUR-IP:{location.port}</code> directly in the browser address bar</li>
          <li>Click <strong>Advanced</strong> &rarr; <strong>Accept the Risk</strong> (or "Proceed to site")<br>
            <span style="color: var(--text-dim); font-size: 11px;">This step is mandatory. Browsers silently block requests to untrusted certs without showing a warning for background requests (like JanitorAI's API calls).</span></li>
          <li>In JanitorAI, set the reverse proxy URL to:<br>
            <code>https://YOUR-IP:{location.port}/v1/chat/completions</code></li>
        </ol>
      </div>
    {/if}
  </div>

{/if}

{#if showUserForm}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showUserForm = false; }}>
    <div class="modal">
      <h3>Add User</h3>
      <form onsubmit={(e) => { e.preventDefault(); createUser(); }}>
        <div class="form-group"><label for="user-name">Username</label><input id="user-name" bind:value={newUsername} required /></div>
        <div class="form-group"><label for="user-pass">Password</label><input id="user-pass" type="password" bind:value={newPassword} required /></div>
        <div class="modal-actions">
          <button onclick={() => showUserForm = false}>Cancel</button>
          <button type="submit" class="primary">Create</button>
        </div>
      </form>
    </div>
  </div>
{/if}

{#if editingUser}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) editingUser = null; }}>
    <div class="modal">
      <h3>Edit User</h3>
      <form onsubmit={(e) => { e.preventDefault(); saveEdit(); }}>
        <div class="form-group">
          <label for="edit-name">Username</label>
          <input id="edit-name" bind:value={editUsername} required />
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
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) resettingPasswordId = null; }}>
    <div class="modal">
      <h3>Reset Password</h3>
      <form onsubmit={(e) => { e.preventDefault(); handleResetPassword(); }}>
        <div class="form-group">
          <label for="reset-pass">New Password</label>
          <input id="reset-pass" type="password" bind:value={resetPasswordValue} required />
        </div>
        <div class="modal-actions">
          <button onclick={() => resettingPasswordId = null}>Cancel</button>
          <button type="submit" class="primary">Reset</button>
        </div>
      </form>
    </div>
  </div>
{/if}
