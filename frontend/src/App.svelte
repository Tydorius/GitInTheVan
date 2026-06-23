<script lang="ts">
  import { isAuthenticated, currentRoute, logout, isAdmin, initializeAuth } from './stores'
  import Login from './pages/Login.svelte'
  import Dashboard from './pages/Dashboard.svelte'
  import Endpoints from './pages/Endpoints.svelte'
  import Cantrips from './pages/Cantrips.svelte'
  import Lorebooks from './pages/Lorebooks.svelte'
  import Verification from './pages/Verification.svelte'
  import Memories from './pages/Memories.svelte'
  import Maps from './pages/Maps.svelte'
  import Packs from './pages/Packs.svelte'
  import Settings from './pages/Settings.svelte'
  import Admin from './pages/Admin.svelte'

  const navItems = [
    { path: '/', label: 'Dashboard', icon: 'D' },
    { path: '/endpoints', label: 'Endpoints', icon: 'E' },
    { path: '/cantrips', label: 'Cantrips', icon: 'C' },
    { path: '/lorebooks', label: 'Lorebooks', icon: 'L' },
    { path: '/verification', label: 'Verification', icon: 'V' },
    { path: '/memories', label: 'Memories', icon: 'M' },
    { path: '/maps', label: 'Maps', icon: 'A' },
    { path: '/packs', label: 'Content Packs', icon: 'P' },
    { path: '/settings', label: 'Settings', icon: 'S' },
    { path: '/admin', label: 'Admin', icon: 'X', admin: true },
  ]

  let sidebarCollapsed = true

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

  initializeAuth()
</script>

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
