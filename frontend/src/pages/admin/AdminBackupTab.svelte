<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '../../api'
  import { downloadFromApi } from '../../lib/download'
  import CollapsibleCard from '../../lib/CollapsibleCard.svelte'
  import Modal from '../../lib/Modal.svelte'
  import type { CollapseController } from '../../lib/collapse'

  let { collapse }: { collapse: CollapseController } = $props()

  let backups: any[] = $state([])
  let backupRunning = $state(false)
  let backupError = $state('')
  let error = $state('')
  let saved = $state(false)
  let backupScheduleForm = $state({
    backup_schedule_enabled: false,
    backup_schedule_days: '',
    backup_schedule_time: '03:00',
    backup_retention_count: 7,
  })
  let restoreConfirmId: string | null = $state(null)
  let restoreToken = $state('')
  let restoring = $state(false)

  async function loadSchedule() {
    try {
      const s = await api.getAdminSettings()
      backupScheduleForm = {
        backup_schedule_enabled: s.backup_schedule_enabled,
        backup_schedule_days: s.backup_schedule_days || '',
        backup_schedule_time: s.backup_schedule_time || '03:00',
        backup_retention_count: s.backup_retention_count,
      }
    } catch (e: any) { error = e.message }
  }

  async function loadBackups() {
    try {
      backups = await api.listBackups()
    } catch (e: any) { backupError = e.message }
  }

  async function runBackupNow() {
    backupError = ''
    backupRunning = true
    try {
      await api.runBackupNow()
      await loadBackups()
    } catch (e: any) { backupError = e.message }
    finally { backupRunning = false }
  }

  async function deleteBackupEntry(id: string) {
    backupError = ''
    try {
      await api.deleteBackup(id)
      await loadBackups()
    } catch (e: any) { backupError = e.message }
  }

  async function downloadBackupEntry(id: string, filePath: string) {
    backupError = ''
    try {
      await downloadFromApi(
        `/api/admin/backup/download/${id}`,
        filePath.split(/[\\/]/).pop() || 'backup',
      )
    } catch (e: any) { backupError = e.message }
  }

  async function startRestore(id: string) {
    backupError = ''
    try {
      const result = await api.requestBackupRestore(id)
      restoreConfirmId = id
      restoreToken = result.token
    } catch (e: any) { backupError = e.message }
  }

  async function confirmRestore() {
    if (!restoreConfirmId) return
    restoring = true
    backupError = ''
    try {
      await api.confirmBackupRestore(restoreConfirmId, restoreToken)
      restoreConfirmId = null
    } catch (e: any) { backupError = e.message }
    finally { restoring = false }
  }

  async function saveBackupSchedule() {
    error = ''; saved = false
    try {
      await api.updateAdminSettings(backupScheduleForm)
      saved = true
      setTimeout(() => saved = false, 2000)
      await loadSchedule()
    } catch (e: any) { error = e.message }
  }

  onMount(() => {
    loadSchedule()
    loadBackups()
  })
</script>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if saved}<div class="success-msg">Settings saved.</div>{/if}
{#if backupError}<div class="error-msg">{backupError}</div>{/if}

<CollapsibleCard title="Backup Schedule" cardKey="backup-schedule" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    Automatic backups run in the background at the configured time. This is a best-effort
    convenience feature, not a replacement for a proper production backup strategy on
    PostgreSQL/MariaDB deployments.
  </p>
  <div class="form-row">
    <div class="form-group">
      <label for="backup-enabled">
        <input id="backup-enabled" type="checkbox" bind:checked={backupScheduleForm.backup_schedule_enabled} style="width: auto; margin-right: 6px;" />
        Enable scheduled backups
      </label>
    </div>
    <div class="form-group">
      <label for="backup-days">Days (comma-separated, e.g. mon,wed,fri; blank = every day)</label>
      <input id="backup-days" type="text" bind:value={backupScheduleForm.backup_schedule_days} placeholder="mon,wed,fri" />
    </div>
    <div class="form-group">
      <label for="backup-time">Time (24h, UTC)</label>
      <input id="backup-time" type="text" bind:value={backupScheduleForm.backup_schedule_time} placeholder="03:00" />
    </div>
    <div class="form-group">
      <label for="backup-retention">Retention Count</label>
      <input id="backup-retention" type="number" min="0" bind:value={backupScheduleForm.backup_retention_count} />
    </div>
  </div>
  <button class="primary" onclick={saveBackupSchedule}>Save Schedule</button>
</CollapsibleCard>

<CollapsibleCard title="Backups" cardKey="backup-list" {collapse}>
  <button class="primary" onclick={runBackupNow} disabled={backupRunning} style="margin-bottom: 16px;">
    {backupRunning ? 'Running...' : 'Run Backup Now'}
  </button>

  {#if backups.length === 0}
    <p style="color: var(--text-dim); font-size: 12px;">No backups yet.</p>
  {:else}
    <table style="width: 100%; font-size: 12px;">
      <thead>
        <tr>
          <th style="text-align: left;">Started</th>
          <th style="text-align: left;">Triggered By</th>
          <th style="text-align: left;">Status</th>
          <th style="text-align: left;">Size</th>
          <th style="text-align: left;">Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each backups as b (b.id)}
          <tr>
            <td>{new Date(b.started_at).toLocaleString()}</td>
            <td>{b.triggered_by}</td>
            <td>{b.status}{#if b.status === 'failed'} - {b.error_message}{/if}</td>
            <td>{b.status === 'success' ? `${(b.size_bytes / 1024).toFixed(1)} KB` : '-'}</td>
            <td>
              {#if b.status === 'success'}
                <button onclick={() => downloadBackupEntry(b.id, b.file_path)}>Download</button>
                <button onclick={() => startRestore(b.id)}>Restore</button>
              {/if}
              <button onclick={() => deleteBackupEntry(b.id)}>Delete</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</CollapsibleCard>

<Modal open={!!restoreConfirmId} title="Confirm Restore" onclose={() => restoreConfirmId = null}>
  <p style="font-size: 13px; margin: 12px 0;">
    This will overwrite the live database with this backup. This action cannot be undone,
    and the server must be restarted afterward for the restored data to take effect.
    Are you sure?
  </p>
  <div style="display: flex; gap: 8px; justify-content: flex-end;">
    <button onclick={() => restoreConfirmId = null}>Cancel</button>
    <button class="primary" onclick={confirmRestore} disabled={restoring}>
      {restoring ? 'Restoring...' : 'Yes, Restore'}
    </button>
  </div>
</Modal>
