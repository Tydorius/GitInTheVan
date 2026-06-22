<script lang="ts">
  import { api, setToken, setApiKey, getToken } from '../api'
  import { isAuthenticated, checkAdmin } from '../stores'

  let username = ''
  let password = ''
  let error = ''
  let loading = false
  let isSetup = false
  let createdApiKey = ''

  async function checkExisting() {
    const token = getToken()
    if (token) {
      isAuthenticated.set(true)
      await checkAdmin()
    }
  }

  async function handleSubmit() {
    error = ''
    loading = true
    try {
      if (isSetup) {
        const data = await api.setup(username, password)
        setToken(data.access_token)
        setApiKey(data.api_key)
        createdApiKey = data.api_key
        isAuthenticated.set(true)
        await checkAdmin()
        window.location.hash = '#/'
      } else {
        const data = await api.login(username, password)
        setToken(data.access_token)
        isAuthenticated.set(true)
        await checkAdmin()
        window.location.hash = '#/'
      }
    } catch (e: any) {
      error = e.message || 'Authentication failed'
    } finally {
      loading = false
    }
  }

  checkExisting()
</script>

<div class="login-container">
  <div class="login-box">
    <h2>{isSetup ? 'Admin Setup' : 'Login'}</h2>
    <p>{isSetup ? 'Create the first admin account' : 'Sign in to manage your proxy'}</p>

    {#if error}<div class="error-msg">{error}</div>{/if}
    {#if createdApiKey}
      <div class="success-msg">
        Admin created! Your API key:
        <div class="api-key-display">{createdApiKey}</div>
        Save this key - it's used for proxy requests.
      </div>
    {/if}

    <form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }} autocomplete="off">
      <div class="form-group">
        <label for="username">Username</label>
        <input id="username" type="text" autocomplete="off" spellcheck="false" bind:value={username} placeholder="admin" required />
      </div>
      <div class="form-group">
        <label for="password">Password</label>
        <input id="password" type="password" autocomplete="new-password" bind:value={password} placeholder="password" required />
      </div>
      <button type="submit" class="primary" disabled={loading} style="width: 100%;">
        {loading ? 'Please wait...' : (isSetup ? 'Create Admin' : 'Login')}
      </button>
    </form>

    <div style="margin-top: 16px; text-align: center;">
      <button onclick={() => isSetup = !isSetup} style="border: none; background: none; color: var(--accent); text-decoration: underline; cursor: pointer; font-size: 12px;">
        {isSetup ? '← Back to login' : 'First run? Setup admin →'}
      </button>
    </div>
  </div>
</div>
