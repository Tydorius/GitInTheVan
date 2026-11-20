<script lang="ts">
  import { routeParams } from '../stores'
  import { CollapseController } from '../lib/collapse'
  import AdminCapsTab from './admin/AdminCapsTab.svelte'
  import AdminUsersTab from './admin/AdminUsersTab.svelte'
  import AdminUpdateTab from './admin/AdminUpdateTab.svelte'
  import AdminAuditTab from './admin/AdminAuditTab.svelte'
  import AdminLogsTab from './admin/AdminLogsTab.svelte'
  import AdminNetworkTab from './admin/AdminNetworkTab.svelte'
  import AdminBackupTab from './admin/AdminBackupTab.svelte'

  let tab = $state('caps')
  const ADMIN_TABS = ['caps', 'users', 'update', 'audit', 'logs', 'network', 'backup']

  // `?tab=backup` etc. opens a sub-tab directly. Unknown values are ignored.
  $effect(() => {
    if (ADMIN_TABS.includes($routeParams.params.tab)) {
      tab = $routeParams.params.tab
    }
  })

  let collapse = new CollapseController('admin', ['caps-limits', 'caps-sizes', 'caps-debug', 'caps-blocklist', 'caps-assistant', 'caps-logging', 'caps-banner', 'logs-viewer', 'network-ssl', 'update-panel', 'backup-schedule', 'backup-list'])
  let updateInfo: any = $state(null)
  let updateChain: any = $state(null)
</script>

<div class="page-header">
  <h2>Admin <a class="help-link" href="/help/user-guide.html#admin" target="_blank" title="Open documentation">?</a></h2>
  <div style="display: flex; gap: 4px; align-items: center;">
    {#if tab === 'caps'}
      <button onclick={() => collapse.setAll(true)}>Collapse All</button>
      <button onclick={() => collapse.setAll(false)}>Expand All</button>
      <span style="width: 1px; height: 20px; background: var(--border); margin: 0 4px;"></span>
    {/if}
    <button onclick={() => tab = 'caps'} class={tab === 'caps' ? 'primary' : ''}>Global Caps</button>
    <button onclick={() => tab = 'users'} class={tab === 'users' ? 'primary' : ''}>Users</button>
    <button onclick={() => tab = 'update'} class={tab === 'update' ? 'primary' : ''}>Update{#if updateChain?.active}<span style="color: #3b82f6; margin-left: 4px; font-weight: bold;">⟳</span>{:else if updateChain?.status && ['failed', 'stalled', 'version_mismatch', 'expired'].includes(updateChain.status)}<span style="color: #ef4444; margin-left: 4px; font-weight: bold;">×</span>{:else if updateInfo?.update_available}<span style="color: #ef4444; margin-left: 4px; font-weight: bold;">⓵</span>{/if}</button>
    <button onclick={() => tab = 'audit'} class={tab === 'audit' ? 'primary' : ''}>Audit Logs</button>
    <button onclick={() => tab = 'logs'} class={tab === 'logs' ? 'primary' : ''}>Server Logs</button>
    <button onclick={() => tab = 'network'} class={tab === 'network' ? 'primary' : ''}>Network</button>
    <button onclick={() => tab = 'backup'} class={tab === 'backup' ? 'primary' : ''}>Backup</button>
  </div>
</div>

{#if tab === 'caps'}
  <AdminCapsTab {collapse} />
{:else if tab === 'users'}
  <AdminUsersTab />
{:else if tab === 'update'}
  <AdminUpdateTab onStatus={(info, chain) => { updateInfo = info; updateChain = chain }} />
{:else if tab === 'audit'}
  <AdminAuditTab />
{:else if tab === 'logs'}
  <AdminLogsTab />
{:else if tab === 'network'}
  <AdminNetworkTab />
{:else if tab === 'backup'}
  <AdminBackupTab {collapse} />
{/if}
