<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '../../api'
  import { loadSiteBanner } from '../../stores'
  import CollapsibleCard from '../../lib/CollapsibleCard.svelte'
  import type { CollapseController } from '../../lib/collapse'

  let { collapse }: { collapse: CollapseController } = $props()

  let loading = $state(true)
  let error = $state('')
  let saved = $state(false)
  let adminSettings: any = $state(null)
  let logLevelInput = $state('')
  let bannerInput = $state('')
  let bannerLevelInput = $state('info')

  let capsForm = $state({
    max_driver_callable_turns: 2,
    max_verification_retries: 3,
    max_map_stages: 3,
    rate_limit_proxy_per_min: 60,
    rate_limit_api_per_min: 120,
    max_memory_size_mb: 50,
    max_script_size_kb: 50,
    max_rule_size_kb: 25,
    max_lorebook_size_kb: 500,
    max_saved_debug_runs: 10,
    max_debug_exchange_kb: 512,
    max_snapshots_per_object: 20,
    metrics_retention_days: 30,
    assistant_cantrip_auto_install_allowed: false,
    passenger_calling_enabled: true,
    max_passenger_calls_per_request: 3,
    passenger_timeout_seconds: 60,
    url_blocklist: '',
    runtime_log_level: '',
    assistant_enabled: true,
    assistant_admin_reads_enabled: false,
    assistant_packs_enabled: false,
    max_assistant_tool_calls_per_turn: 16,
    max_assistant_conversations: 20,
    max_assistant_tool_result_kb: 32,
  })

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
        max_memory_size_mb: adminSettings.max_memory_size_mb,
        max_script_size_kb: adminSettings.max_script_size_kb,
        max_rule_size_kb: adminSettings.max_rule_size_kb,
        max_lorebook_size_kb: adminSettings.max_lorebook_size_kb,
        max_saved_debug_runs: adminSettings.max_saved_debug_runs,
        max_debug_exchange_kb: adminSettings.max_debug_exchange_kb,
        max_snapshots_per_object: adminSettings.max_snapshots_per_object,
        metrics_retention_days: adminSettings.metrics_retention_days,
        assistant_cantrip_auto_install_allowed: (adminSettings as any).assistant_cantrip_auto_install_allowed ?? false,
        passenger_calling_enabled: (adminSettings as any).passenger_calling_enabled ?? true,
        max_passenger_calls_per_request: (adminSettings as any).max_passenger_calls_per_request ?? 3,
        passenger_timeout_seconds: (adminSettings as any).passenger_timeout_seconds ?? 60,
        url_blocklist: adminSettings.url_blocklist || '',
        runtime_log_level: adminSettings.runtime_log_level || '',
        assistant_enabled: adminSettings.assistant_enabled,
        assistant_admin_reads_enabled: adminSettings.assistant_admin_reads_enabled,
        assistant_packs_enabled: adminSettings.assistant_packs_enabled,
        max_assistant_tool_calls_per_turn: adminSettings.max_assistant_tool_calls_per_turn,
        max_assistant_conversations: adminSettings.max_assistant_conversations,
        max_assistant_tool_result_kb: adminSettings.max_assistant_tool_result_kb,
      }
      logLevelInput = adminSettings.runtime_log_level || ''
      bannerInput = adminSettings.site_banner || ''
      bannerLevelInput = adminSettings.site_banner_level || 'info'
    } catch (e: any) { error = e.message }
    finally { loading = false }
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
    } catch (e: any) { error = e.message }
  }

  async function clearLogLevel() {
    logLevelInput = ''
    await applyLogLevel()
  }

  async function saveBanner() {
    error = ''; saved = false
    try {
      await api.updateAdminSettings({ site_banner: bannerInput, site_banner_level: bannerLevelInput })
      saved = true
      setTimeout(() => saved = false, 2000)
      await load()
      await loadSiteBanner()
    } catch (e: any) { error = e.message }
  }

  async function clearBanner() {
    bannerInput = ''
    await saveBanner()
  }

  onMount(load)
</script>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if saved}<div class="success-msg">Settings saved.</div>{/if}

{#if loading}<div class="loading">Loading...</div>
{:else}
  <CollapsibleCard title="Global Limits" cardKey="caps-limits" {collapse}>
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
    <div class="form-row">
      <div class="form-group">
        <label for="cap-psg-on">
          <input id="cap-psg-on" type="checkbox" bind:checked={capsForm.passenger_calling_enabled} style="width: auto;" />
          Passenger Calling Enabled
        </label>
        <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
          The server-wide kill switch for scripted LLM calls from cantrips. With it off,
          <code>context.passenger.call()</code> returns null for every user.
        </p>
      </div>
      <div class="form-group">
        <label for="cap-psg-calls">Max Passenger Calls Per Request</label>
        <input id="cap-psg-calls" type="number" bind:value={capsForm.max_passenger_calls_per_request} min="0" />
        <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
          Ceiling shared by every cantrip and map stage in one request. A user's own budget
          (Settings) can lower it, never raise it.
        </p>
      </div>
      <div class="form-group">
        <label for="cap-psg-timeout">Passenger Call Timeout (seconds)</label>
        <input id="cap-psg-timeout" type="number" bind:value={capsForm.passenger_timeout_seconds} min="1" />
      </div>
    </div>
    <button class="primary" onclick={saveCaps}>Save</button>
  </CollapsibleCard>

  <CollapsibleCard title="Content Size Limits" cardKey="caps-sizes" {collapse}>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Maximum size of user-authored content. Enforced when a resource is created or
      updated, and when a content pack is installed.
    </p>
    <div class="form-row">
      <div class="form-group">
        <label for="cap-memory">Max Memory Store (MB per user)</label>
        <input id="cap-memory" type="number" bind:value={capsForm.max_memory_size_mb} min="1" />
      </div>
      <div class="form-group">
        <label for="cap-script">Max Cantrip Code (KB)</label>
        <input id="cap-script" type="number" bind:value={capsForm.max_script_size_kb} min="1" />
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label for="cap-rule">Max Verification Rule (KB)</label>
        <input id="cap-rule" type="number" bind:value={capsForm.max_rule_size_kb} min="1" />
      </div>
      <div class="form-group">
        <label for="cap-lorebook">Max Lorebook (KB)</label>
        <input id="cap-lorebook" type="number" bind:value={capsForm.max_lorebook_size_kb} min="1" />
      </div>
    </div>
    <button class="primary" onclick={saveCaps}>Save</button>
  </CollapsibleCard>

  <CollapsibleCard title="Debug Runs" cardKey="caps-debug" {collapse}>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Users capture debug runs by enabling Debug Mode in their own Settings. Recent runs
      are pruned automatically; saved runs are exempt so a comparison baseline is not
      lost to later traffic.
    </p>
    <div class="form-row">
      <div class="form-group">
        <label for="cap-saved-runs">Max Saved Runs (per user)</label>
        <input id="cap-saved-runs" type="number" bind:value={capsForm.max_saved_debug_runs} min="0" />
      </div>
      <div class="form-group">
        <label for="cap-debug-kb">Max Size Per Run (KB)</label>
        <input id="cap-debug-kb" type="number" bind:value={capsForm.max_debug_exchange_kb} min="1" />
      </div>
      <div class="form-group">
        <label for="cap-snapshots">Max Snapshots Per Object</label>
        <input id="cap-snapshots" type="number" bind:value={capsForm.max_snapshots_per_object} min="1" />
      </div>
      <div class="form-group">
        <label for="cap-metrics-retention">Metrics Retention (days)</label>
        <input id="cap-metrics-retention" type="number" bind:value={capsForm.metrics_retention_days} min="1" />
      </div>
    </div>
    <p style="color: var(--text-dim); font-size: 12px;">
      A run larger than the size limit keeps its reasoning, cantrip output and metrics;
      the per-stage message snapshots are dropped first and the run is marked as
      truncated rather than being silently trimmed.
    </p>
    <button class="primary" onclick={saveCaps}>Save</button>
  </CollapsibleCard>

  <CollapsibleCard title="URL Blocklist" cardKey="caps-blocklist" {collapse}>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Comma-separated domains that user content may not reference and that endpoints may
      not point at. Leave blank to allow everything.
    </p>
    <div class="form-group">
      <label for="cap-blocklist">Blocked Domains</label>
      <input id="cap-blocklist" type="text" bind:value={capsForm.url_blocklist}
             placeholder="evil.example, tracker.example.net" />
    </div>
    <button class="primary" onclick={saveCaps}>Save</button>
  </CollapsibleCard>

  <CollapsibleCard title="Assistant" cardKey="caps-assistant" {collapse}>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Server-wide switches and limits for the Assistant Pane. Per-user endpoint/model choice is
      in each user's own Settings; per-tool permissions are in Assistant Security.
    </p>
    <div class="form-group">
      <label for="cap-assistant-enabled">
        <input id="cap-assistant-enabled" type="checkbox" bind:checked={capsForm.assistant_enabled} style="width: auto; margin-right: 6px;" />
        Enable the Assistant Pane
      </label>
    </div>
    <div class="form-group">
      <label for="cap-assistant-admin-reads">
        <input id="cap-assistant-admin-reads" type="checkbox" bind:checked={capsForm.assistant_admin_reads_enabled} style="width: auto; margin-right: 6px;" />
        Allow the assistant to read server logs and schema state (admins only)
      </label>
    </div>
    <div class="form-group">
      <label for="cap-assistant-packs">
        <input id="cap-assistant-packs" type="checkbox" bind:checked={capsForm.assistant_packs_enabled} style="width: auto; margin-right: 6px;" />
        Allow the assistant to link repos and install content packs
      </label>
      <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
        Off by default. The assistant has no web search of its own, so a repo name it suggests
        could be hallucinated -- exactly the shape of a slopsquatted package.
      </p>
    </div>
    <div class="form-group">
      <label for="cap-cantrip-auto-install">
        <input id="cap-cantrip-auto-install" type="checkbox" bind:checked={capsForm.assistant_cantrip_auto_install_allowed} style="width: auto; margin-right: 6px;" />
        Allow in-chat cantrip auto-install (<code>&lt;CANTRIP:...&gt;</code>)
      </label>
      <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
        Admin half of a doubly-gated switch; the user enables their half in Settings. With both
        on, a <code>&lt;CANTRIP:...&gt;</code> command drafts, validates, smoke-tests and installs
        the cantrip inside the request instead of queueing a draft for the pane. Drafted code
        always passes the safety scanner and a sandbox smoke run before it can install enabled.
      </p>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label for="cap-assistant-turn">Max Tool Calls Per Turn</label>
        <input id="cap-assistant-turn" type="number" bind:value={capsForm.max_assistant_tool_calls_per_turn} min="1" />
        <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
          When hit, the turn ends and the pane shows Continue instead of looping unattended.
        </p>
      </div>
      <div class="form-group">
        <label for="cap-assistant-convos">Max Conversations (unsaved, per user)</label>
        <input id="cap-assistant-convos" type="number" bind:value={capsForm.max_assistant_conversations} min="1" />
      </div>
      <div class="form-group">
        <label for="cap-assistant-result">Max Tool Result Size (KB)</label>
        <input id="cap-assistant-result" type="number" bind:value={capsForm.max_assistant_tool_result_kb} min="1" />
      </div>
    </div>
    <button class="primary" onclick={saveCaps}>Save</button>
  </CollapsibleCard>

  <CollapsibleCard title="Runtime Log Level" cardKey="caps-logging" {collapse}>
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
  </CollapsibleCard>

  <CollapsibleCard title="Site Banner" cardKey="caps-banner" {collapse}>
    <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
      Shows a sitewide message to all users, including on the login page. Leave the message blank to hide the banner.
    </p>
    <div class="form-row">
      <div class="form-group" style="flex: 2;">
        <label for="banner-text">Banner Message</label>
        <input id="banner-text" type="text" bind:value={bannerInput} placeholder="e.g. Scheduled maintenance tonight at 10pm" />
      </div>
      <div class="form-group">
        <label for="banner-level">Level</label>
        <select id="banner-level" bind:value={bannerLevelInput}>
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="danger">Danger</option>
        </select>
      </div>
      <div class="form-group" style="display: flex; align-items: flex-end; gap: 8px;">
        <button class="primary" onclick={saveBanner}>Save</button>
        {#if bannerInput}<button onclick={clearBanner}>Clear</button>{/if}
      </div>
    </div>
  </CollapsibleCard>
{/if}
