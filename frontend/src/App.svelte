<script lang="ts">
  import { isAuthenticated, currentRoute, logout, isAdmin, initializeAuth, siteBanner, loadSiteBanner,
           certIpWarning, loadCertIpWarning, acknowledgeCertIpWarning } from './stores'
  import { onMount } from 'svelte'
  import { api } from './api'
  import Login from './pages/Login.svelte'
  import Dashboard from './pages/Dashboard.svelte'
  import Endpoints from './pages/Endpoints.svelte'
  import Cantrips from './pages/Cantrips.svelte'
  import Lorebooks from './pages/Lorebooks.svelte'
  import Skills from './pages/Skills.svelte'
  import TagGroups from './pages/TagGroups.svelte'
  import Verification from './pages/Verification.svelte'
  import Memories from './pages/Memories.svelte'
  import Maps from './pages/Maps.svelte'
  import Packs from './pages/Packs.svelte'
  import Settings from './pages/Settings.svelte'
  import Admin from './pages/Admin.svelte'

  const navItems = [
    { path: '/', label: 'Dashboard', icon: '◧' },
    { path: '/endpoints', label: 'Endpoints', icon: '⇄' },
    { path: '/cantrips', label: 'Cantrips', icon: '⚡' },
    { path: '/lorebooks', label: 'Lorebooks', icon: '📖' },
    { path: '/skills', label: 'Skills & Samples', icon: '✎' },
    { path: '/tags', label: 'Tags and Groups', icon: '🏷' },
    { path: '/verification', label: 'Verification', icon: '✓' },
    { path: '/memories', label: 'Memory', icon: '🧠' },
    { path: '/maps', label: 'Maps', icon: '🗺' },
    { path: '/packs', label: 'Content Packs', icon: '📦' },
    { path: '/settings', label: 'Settings', icon: '⚙' },
    { path: '/admin', label: 'Admin', icon: '🛡', admin: true },
  ]

  let sidebarCollapsed = true
  let updateAvailable = false
  let latestVersion = ''
  let certAckChecked = false
  let certAckBusy = false

  async function checkForUpdates() {
    if (!$isAdmin) return
    try {
      const data = await api.checkUpdate()
      updateAvailable = data.update_available
      latestVersion = data.latest_version
    } catch {}
  }

  async function acknowledgeCertWarning() {
    if (!certAckChecked || !$certIpWarning) return
    certAckBusy = true
    try {
      await acknowledgeCertIpWarning($certIpWarning.fingerprint)
      certAckChecked = false
    } catch {}
    finally { certAckBusy = false }
  }

  const bannerColors: Record<string, string> = {
    info: '#2563eb',
    warning: '#d97706',
    danger: '#dc2626',
  }

  onMount(() => {
    initializeAuth()
    loadSiteBanner()
    setTimeout(checkForUpdates, 3000)
    setInterval(checkForUpdates, 300000)
    setInterval(loadCertIpWarning, 300000)
  })
  function handleLogout() {
    logout()
  }

  function toggleSidebar() {
    sidebarCollapsed = !sidebarCollapsed
  }

  function closeSidebar() {
    sidebarCollapsed = true
  }

  $: route = $currentRoute || '/'
  $: page = route.split('?')[0]
</script>

{#if $siteBanner}
  <div class="site-banner" style="background: {bannerColors[$siteBanner.level] || bannerColors.info};">
    {$siteBanner.banner}
  </div>
{/if}
{#if $isAuthenticated && $isAdmin && $certIpWarning}
  <div class="cert-warning-banner">
    <div class="cert-warning-body">
      <strong>HTTPS certificate no longer matches this machine's network address.</strong>
      <div class="cert-warning-detail">
        Certificate was issued for <code>{$certIpWarning.cert_ips.join(', ') || 'no IP address'}</code>,
        but this machine is now on <code>{$certIpWarning.local_ips.join(', ') || 'no LAN address'}</code>.
        Devices connecting over HTTPS will fail certificate validation until this is resolved.
      </div>
      <div class="cert-warning-detail">
        If the address changed by accident &mdash; a router reboot reissuing DHCP leases, for example &mdash;
        putting this machine back on <code>{$certIpWarning.cert_ips.join(', ')}</code> with a static IP or a
        DHCP reservation fixes it and keeps the certificate your devices already trust.
        Regenerating from <strong>Admin &rarr; Network</strong> also works, but every device has to
        accept the new certificate again.
      </div>
    </div>
    <div class="cert-warning-ack">
      <label>
        <input type="checkbox" bind:checked={certAckChecked} />
        I understand &mdash; hide this until the server restarts
      </label>
      <button onclick={acknowledgeCertWarning} disabled={!certAckChecked || certAckBusy}>
        {certAckBusy ? 'Dismissing...' : 'Acknowledge'}
      </button>
    </div>
  </div>
{/if}
{#if !$isAuthenticated}
  <Login />
{:else if $isAuthenticated && (page === '/login' || page === '')}
  <Dashboard />
{:else}
  <div class="app-layout" class:sidebar-collapsed={sidebarCollapsed}>
    <aside class="sidebar">
      <div class="sidebar-header" onclick={toggleSidebar} role="button" tabindex="0">
        <img src="/gitinthevan-full.svg" alt="GitInTheVan LLM Router & Proxy" style="max-width: 100%;" />
        <span class="mobile-header">{sidebarCollapsed ? '☰' : '✕'}</span>
      </div>
      <nav class="sidebar-nav">
        {#each navItems as item}
          {#if !item.admin || $isAdmin}
            <a href={`#${item.path}`} class={page === item.path ? 'active' : ''} onclick={closeSidebar}>
              <span>{item.icon}</span> {item.label}
              {#if item.admin && updateAvailable}
                <span style="margin-left: 4px; color: #ef4444; font-weight: bold; position: relative;">⓵</span>
              {/if}
            </a>
          {/if}
        {/each}
      </nav>
      <div class="sidebar-footer">
        <button onclick={handleLogout} style="width: 100%;">Logout</button>
      </div>
    </aside>

    <main class="main-content">
      {#if page === '/'}
        <Dashboard />
      {:else if page === '/endpoints'}
        <Endpoints />
      {:else if page === '/cantrips'}
        <Cantrips />
      {:else if page === '/lorebooks'}
        <Lorebooks />
      {:else if page === '/skills'}
        <Skills />
      {:else if page === '/tags'}
        <TagGroups />
      {:else if page === '/verification'}
        <Verification />
      {:else if page === '/memories'}
        <Memories />
      {:else if page === '/maps'}
        <Maps />
      {:else if page === '/packs'}
        <Packs />
      {:else if page === '/settings'}
        <Settings />
      {:else if page === '/admin'}
        <Admin />
      {:else}
        <Dashboard />
      {/if}
    </main>
  </div>
{/if}
