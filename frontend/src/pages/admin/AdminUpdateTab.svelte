<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { api } from '../../api'

  let { onStatus }: { onStatus?: (info: any, chain: any) => void } = $props()

  let updateInfo: any = $state(null)
  let updateChecking = $state(false)
  let updateError = $state('')
  let updating = $state(false)
  let updateLaunched = $state(false)
  let updateChain: any = $state(null)
  let chainInterval: ReturnType<typeof setInterval> | null = null
  let chainBusy = $state(false)
  let showConfirmModal = $state(false)
  let approveAll = $state(true)
  let resumeApproveAll = $state(true)

  async function checkUpdate() {
    updateChecking = true; updateError = ''
    try {
      updateInfo = await api.checkUpdate()
      onStatus?.(updateInfo, updateChain)
    } catch (e: any) { updateError = e.message }
    finally { updateChecking = false }
  }

  async function loadChain() {
    try {
      updateChain = await api.getUpdateChain()
      onStatus?.(updateInfo, updateChain)
      if (updateChain?.active && !chainInterval) {
        chainInterval = setInterval(loadChain, 5000)
      } else if (!updateChain?.active && chainInterval) {
        clearInterval(chainInterval)
        chainInterval = null
      }
    } catch { /* chain state is advisory; a failure here must not break the tab */ }
  }

  function openUpdateConfirm() {
    approveAll = true
    updateError = ''
    showConfirmModal = true
  }

  async function confirmLaunchUpdate() {
    showConfirmModal = false
    updating = true
    updateError = ''
    try {
      const result = await api.executeUpdate({ auto_approve: approveAll })
      if (result.success) {
        updateLaunched = true
        loadChain()
      } else {
        updateError = result.error || 'Update failed'
      }
    } catch (e: any) { updateError = e.message }
    finally { updating = false }
  }

  async function resumeChain(autoApprove: boolean | undefined = undefined) {
    chainBusy = true; updateError = ''
    try {
      const opts = autoApprove === undefined ? undefined : { auto_approve: autoApprove }
      const result = await api.resumeUpdateChain(opts)
      if (!result.success) updateError = result.error || 'Could not resume update'
      await loadChain()
    } catch (e: any) { updateError = e.message }
    finally { chainBusy = false }
  }

  async function abortChain() {
    if (!confirm('Discard the pending update plan? The version already installed is kept.')) return
    chainBusy = true; updateError = ''
    try {
      await api.abortUpdateChain()
      await loadChain()
      await checkUpdate()
    } catch (e: any) { updateError = e.message }
    finally { chainBusy = false }
  }

  onMount(() => {
    checkUpdate()
    loadChain()
  })

  onDestroy(() => {
    if (chainInterval) clearInterval(chainInterval)
  })
</script>

<div class="card">
  <h3>Update GitInTheVan</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    Check for updates from the official GitHub repository.
  </p>

  {#if updateChecking}
    <div class="loading">Checking for updates...</div>
  {:else if updateError && !updateInfo}
    <div class="error-msg">{updateError}</div>
    <button onclick={checkUpdate} style="margin-top: 8px;">Retry</button>
  {:else if updateChain?.active || updateLaunched}
    <div style="background: rgba(59, 130, 246, 0.1); border: 1px solid #3b82f6; border-radius: 4px; padding: 16px; margin-bottom: 16px;">
      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
        <span style="font-size: 18px;">⟳</span>
        <strong style="font-size: 14px; color: #3b82f6;">
          {#if updateChain?.total_steps > 1}
            Update in Progress &mdash; step {updateChain.current_step + 1} of {updateChain.total_steps}
          {:else}
            Update in Progress
          {/if}
        </strong>
      </div>
      {#if updateChain?.total_steps > 1}
        <div style="font-size: 13px; margin-bottom: 8px;">
          Upgrading <code>{updateChain.from_version}</code> &rarr; <code>{updateChain.target_version}</code>
          one release at a time. Each release can only migrate the database from the one
          immediately before it, so the server restarts after every step.
        </div>
        <ol style="font-size: 12px; line-height: 1.9; padding-left: 20px; margin: 8px 0;">
          {#each updateChain.steps as step}
            <li>
              <code>{step.version}</code>
              {#if step.status === 'installed'}<span style="color: #22c55e;">&nbsp;&check; installed</span>
              {:else if step.status === 'launched'}<span style="color: #3b82f6;">&nbsp;&hellip; installing</span>
              {:else if step.status === 'failed'}<span style="color: #ef4444;">&nbsp;&times; failed</span>
              {:else}<span style="color: var(--text-dim);">&nbsp;pending</span>{/if}
            </li>
          {/each}
        </ol>
      {/if}
      <div style="font-size: 13px; line-height: 1.8;">
        The update script stops the server, backs up the database, extracts the new
        version, reinstalls dependencies, rebuilds the frontend, and restarts.<br>
        <strong>The page will lose connection shortly.</strong> Wait about 1-2 minutes per
        step, then refresh.
        {#if updateChain?.total_steps > 1}
          <br><span style="color: var(--text-dim); font-size: 12px;">
            On Windows each step opens its own console window. That is expected.
          </span>
        {/if}
      </div>
    </div>
  {:else if updateChain && updateChain.status === 'awaiting_approval' && !updateChain.active}
    <div style="background: rgba(245, 158, 11, 0.1); border: 1px solid #f59e0b; border-radius: 4px; padding: 16px; margin-bottom: 16px;">
      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
        <span style="font-size: 18px;">⏸</span>
        <strong style="font-size: 14px; color: #f59e0b;">
          Approval Needed &mdash; step {updateChain.current_step + 1} of {updateChain.total_steps}
        </strong>
      </div>
      <div style="font-size: 13px; margin-bottom: 8px;">
        Upgrading <code>{updateChain.from_version}</code> &rarr; <code>{updateChain.target_version}</code>.
        Step {updateChain.current_step} finished; ready to install the next version.
      </div>
      {#if updateChain.steps?.length}
        <ol style="font-size: 12px; line-height: 1.9; padding-left: 20px; margin: 8px 0;">
          {#each updateChain.steps as step}
            <li>
              <code>{step.version}</code>
              {#if step.status === 'installed'}<span style="color: #22c55e;">&nbsp;&check; installed</span>
              {:else if step.status === 'failed'}<span style="color: #ef4444;">&nbsp;&times; failed</span>
              {:else}<span style="color: var(--text-dim);">&nbsp;pending</span>{/if}
            </li>
          {/each}
        </ol>
      {/if}
      <label style="display: flex; align-items: center; gap: 8px; font-size: 12px; margin: 10px 0 12px; cursor: pointer;">
        <input type="checkbox" bind:checked={resumeApproveAll} style="width: auto;" />
        Approve all remaining updates automatically (only stop on error)
      </label>
      <div style="display: flex; gap: 8px; flex-wrap: wrap;">
        <button class="primary" onclick={() => resumeChain(resumeApproveAll)} disabled={chainBusy} style="font-size: 13px; padding: 6px 16px;">
          {chainBusy ? 'Working...' : (resumeApproveAll ? 'Approve All & Continue' : 'Approve Next Step')}
        </button>
        <button onclick={abortChain} disabled={chainBusy} style="font-size: 13px; padding: 6px 16px;">
          Discard plan
        </button>
      </div>
    </div>
  {:else if updateChain && updateChain.status && !updateChain.active && ['failed', 'stalled', 'version_mismatch', 'expired'].includes(updateChain.status)}
    <div style="background: rgba(239, 68, 68, 0.1); border: 1px solid #ef4444; border-radius: 4px; padding: 16px; margin-bottom: 16px;">
      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
        <span style="font-size: 18px;">&times;</span>
        <strong style="font-size: 14px; color: #ef4444;">Update did not finish</strong>
      </div>
      <div style="font-size: 13px; margin-bottom: 8px;">
        Upgrading <code>{updateChain.from_version}</code> &rarr; <code>{updateChain.target_version}</code>
        stopped at step {updateChain.current_step + 1} of {updateChain.total_steps}.
      </div>
      {#if updateChain.error}
        <div class="error-msg" style="margin-bottom: 8px;">{updateChain.error}</div>
      {/if}
      {#if updateChain.log_tail?.length}
        <details style="margin-bottom: 8px;">
          <summary style="font-size: 12px; cursor: pointer; color: var(--text-dim);">Update log</summary>
          <pre style="font-size: 11px; white-space: pre-wrap; margin-top: 8px; max-height: 240px; overflow-y: auto; background: var(--bg-elevated); padding: 12px; border-radius: 4px;">{updateChain.log_tail.join('\n')}</pre>
        </details>
      {/if}
      <div style="display: flex; gap: 8px; flex-wrap: wrap;">
        <button class="primary" onclick={() => resumeChain()} disabled={chainBusy} style="font-size: 13px; padding: 6px 16px;">
          {chainBusy ? 'Working...' : 'Retry this step'}
        </button>
        <button onclick={abortChain} disabled={chainBusy} style="font-size: 13px; padding: 6px 16px;">
          Discard plan
        </button>
      </div>
      <div style="margin-top: 12px; font-size: 11px; color: var(--text-dim);">
        If the server will not start at all, delete <code>data/update-chain.json</code> and
        re-run the deploy script for your platform. Each step's database backup is in
        <code>data/</code>.
      </div>
    </div>
  {:else if updateInfo?.update_available}
    <div style="background: rgba(239, 68, 68, 0.1); border: 1px solid #ef4444; border-radius: 4px; padding: 16px; margin-bottom: 16px;">
      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
        <span style="font-size: 18px;">⓵</span>
        <strong style="font-size: 14px; color: #ef4444;">Update Available!</strong>
      </div>
      <div style="font-size: 13px; margin-bottom: 8px;">
        Current version: <code>{updateInfo.current_version}</code>
        &rarr; Latest version: <code>{updateInfo.latest_version}</code>
      </div>
      {#if updateInfo.step_count > 1}
        <div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">
          You are {updateInfo.step_count} releases behind. The update will install them in
          sequence, restarting after each one, because a release can only migrate the
          database from the release immediately before it.
        </div>
      {/if}
      {#if updateInfo.release_notes}
        <details style="margin-top: 8px;">
          <summary style="font-size: 12px; cursor: pointer; color: var(--text-dim);">Release Notes</summary>
          <div style="font-size: 12px; white-space: pre-wrap; margin-top: 8px; max-height: 300px; overflow-y: auto; background: var(--bg-elevated); padding: 12px; border-radius: 4px;">
            {updateInfo.release_notes}
          </div>
        </details>
      {/if}
      {#if updateError}
        <div class="error-msg" style="margin-top: 8px;">{updateError}</div>
      {/if}
      <div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap;">
        <button class="primary" onclick={openUpdateConfirm} disabled={updating} style="font-size: 13px; padding: 6px 16px;">
          {updating ? 'Downloading...' : 'Update Now'}
        </button>
        {#if updateInfo.zip_url}
          <a href={updateInfo.zip_url} target="_blank" style="text-decoration: none; padding: 6px 16px; border-radius: 4px; font-size: 13px;">
            Download (.zip)
          </a>
        {/if}
        {#if updateInfo.release_url}
          <a href={updateInfo.release_url} target="_blank" style="padding: 6px 16px; font-size: 13px;">
            View on GitHub
          </a>
        {/if}
      </div>
    </div>

    <div style="margin-top: 16px;">
      <details>
        <summary style="font-size: 12px; cursor: pointer; color: var(--text-dim);">Manual Update Instructions (Local Install)</summary>
        <ol style="font-size: 12px; line-height: 2; padding-left: 20px; color: var(--text-dim); margin-top: 8px;">
          <li>Download the update zip using the button above</li>
          <li><strong>Stop the running server</strong> (Ctrl+C in the server terminal)</li>
          <li><strong>Back up your database</strong>: copy <code>data/gitinthevan.db</code> to a safe location</li>
          <li>Extract the zip file over your existing GitInTheVan folder, overwriting all files</li>
          <li>Run the update script for your platform:
            <ul style="margin: 4px 0; padding-left: 20px;">
              <li><strong>Windows</strong>: Double-click <code>scripts\update-windows.bat</code></li>
              <li><strong>macOS</strong>: Run <code>./scripts/update-macos.sh</code> in Terminal</li>
              <li><strong>Linux</strong>: Run <code>./scripts/update-linux.sh</code> in Terminal</li>
            </ul>
          </li>
          <li>The script will reinstall dependencies, rebuild the frontend, and start the server</li>
        </ol>
        <p style="font-size: 12px; line-height: 1.7; color: var(--text-dim); margin-top: 8px;">
          <strong>If you are more than one release behind</strong>, update one release at a
          time. A release only guarantees it can migrate the database from the release
          immediately before it, so skipping releases can leave the database missing columns.
          Versions from 0.19.0 onward do this automatically; for older installs, run
          <code>scripts/chain-update.py</code> from the repository, which performs the whole
          sequence for you.
        </p>
      </details>
    </div>

    <div style="margin-top: 16px; padding: 12px; background: var(--bg-elevated); border-radius: 4px; font-size: 11px; color: var(--text-dim);">
      <strong>Docker/Container updates:</strong> Pull the latest image with <code>docker pull ghcr.io/tydorius/gitinthevan:latest</code>,
      then recreate the container. Your database (in the mounted volume) is preserved automatically.
    </div>
  {:else if updateInfo}
    <div style="background: rgba(34, 197, 94, 0.1); border: 1px solid #22c55e; border-radius: 4px; padding: 16px;">
      <div style="display: flex; align-items: center; gap: 8px;">
        <span style="font-size: 18px;">✓</span>
        <strong style="color: #22c55e;">You're up to date!</strong>
      </div>
      <div style="font-size: 13px; margin-top: 4px;">
        Running version <code>{updateInfo.current_version}</code>
      </div>
    </div>
    <button onclick={checkUpdate} style="margin-top: 12px;">Check Again</button>
  {:else}
    <button class="primary" onclick={checkUpdate}>Check for Updates</button>
  {/if}
</div>

{#if showConfirmModal}
  <div
    class="modal-overlay"
    role="dialog"
    aria-modal="true"
    aria-labelledby="update-confirm-title"
    tabindex="-1"
    onclick={(e) => { if (e.target === e.currentTarget) showConfirmModal = false }}
    onkeydown={(e) => { if (e.key === 'Escape') showConfirmModal = false }}
  >
    <div class="modal" style="max-width: 480px;">
      <h3 id="update-confirm-title">Approve Update</h3>
      <p style="font-size: 13px; line-height: 1.6; margin: 10px 0;">
        {#if (updateInfo?.step_count || 1) > 1}
          This will install <strong>{updateInfo.step_count} updates</strong> in sequence,
          ending at <code>{updateInfo.latest_version}</code>, restarting the server after each step.
        {:else}
          This will install <code>{updateInfo?.latest_version || 'the update'}</code> and restart the server.
        {/if}
      </p>
      <p style="font-size: 12px; color: var(--text-dim); margin: 0 0 12px;">
        The page will lose connection temporarily while the server restarts.
      </p>
      {#if (updateInfo?.step_count || 1) > 1}
        <label style="display: flex; align-items: flex-start; gap: 8px; font-size: 12px; margin: 12px 0; padding: 10px; background: var(--bg-elevated); border-radius: 4px; cursor: pointer;">
          <input type="checkbox" bind:checked={approveAll} style="width: auto; margin-top: 2px;" />
          <span>
            <strong>Approve all updates in this batch</strong><br />
            <span style="color: var(--text-dim);">
              Run all {updateInfo.step_count} steps automatically and only stop if an error occurs.
              Uncheck to pause for your approval after each version.
            </span>
          </span>
        </label>
      {/if}
      <div style="display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px;">
        <button type="button" onclick={() => { showConfirmModal = false }}>Cancel</button>
        <button type="button" class="primary" onclick={confirmLaunchUpdate}>
          {(updateInfo?.step_count || 1) > 1 && approveAll ? 'Approve All & Update' : 'Approve & Update'}
        </button>
      </div>
    </div>
  </div>
{/if}
