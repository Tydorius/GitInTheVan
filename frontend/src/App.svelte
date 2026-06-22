<script lang="ts">
  import { isAuthenticated, currentRoute, logout, isAdmin, initializeAuth } from './stores'
  import Login from './pages/Login.svelte'
  import Dashboard from './pages/Dashboard.svelte'
  import Endpoints from './pages/Endpoints.svelte'
  import Cantrips from './pages/Cantrips.svelte'
  import Lorebooks from './pages/Lorebooks.svelte'
  import Verification from './pages/Verification.svelte'
  import Memories from './pages/Memories.svelte'
  import Packs from './pages/Packs.svelte'
  import Settings from './pages/Settings.svelte'
  import Users from './pages/Users.svelte'

  const navItems = [
    { path: '/', label: 'Dashboard', icon: 'D' },
    { path: '/endpoints', label: 'Endpoints', icon: 'E' },
    { path: '/cantrips', label: 'Cantrips', icon: 'C' },
    { path: '/lorebooks', label: 'Lorebooks', icon: 'L' },
    { path: '/verification', label: 'Verification', icon: 'V' },
    { path: '/memories', label: 'Memories', icon: 'M' },
    { path: '/packs', label: 'Content Packs', icon: 'P' },
    { path: '/settings', label: 'Settings', icon: 'G' },
    { path: '/users', label: 'Users', icon: 'U', admin: true },
  ]

  function handleLogout() {
    logout()
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
  <div class="app-layout">
    <aside class="sidebar">
      <div class="sidebar-header">
        <img src="/gitinthevan-full.svg" alt="GitInTheVan" style="width: 100%; margin-bottom: 8px;" />
        <p>LLM Proxy Router</p>
      </div>
      <nav class="sidebar-nav">
        {#each navItems as item}
          {#if !item.admin || $isAdmin}
            <a href={`#${item.path}`} class={page === item.path ? 'active' : ''}>
              <span>{item.icon}</span> {item.label}
            </a>
          {/if}
        {/each}
      </nav>
      <div style="padding: 16px 20px; border-top: 1px solid var(--border);">
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
      {:else if page === '/packs'}
        <Packs />
      {:else if page === '/settings'}
        <Settings />
      {:else if page === '/users'}
        <Users />
      {:else}
        <Dashboard />
      {/if}
    </main>
  </div>
{/if}
