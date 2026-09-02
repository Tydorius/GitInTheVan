<script lang="ts">
  import { api } from '../api'
  import type { AssistantGroupPermission, AssistantMode, AssistantToolMode } from '../api'
  import { onMount } from 'svelte'
  import CollapsibleCard from '../lib/CollapsibleCard.svelte'
  import { CollapseController } from '../lib/collapse'

  let groups: AssistantGroupPermission[] = []
  let loading = true
  let error = ''
  let saved = false
  let collapse = new CollapseController('assistant-security', [])

  const MODES: { value: AssistantMode; label: string }[] = [
    { value: 'deny', label: 'Deny' },
    { value: 'always_ask', label: 'Always Ask' },
    { value: 'normal', label: 'Normal' },
    { value: 'always_allow', label: 'Always Allow' },
  ]
  const TOOL_MODES: { value: AssistantToolMode; label: string }[] = [
    { value: 'inherit', label: 'Inherit' },
    ...MODES,
  ]

  function modeLabel(mode: string): string {
    return mode.replace('_', ' ')
  }

  // Mirrors permissions.py's resolution order: the tool's own mode wins when
  // it is set to anything but Inherit; otherwise fall back to the group's
  // mode, or the group's default_mode when the group itself is unset. This
  // is why an explicit tool override can un-deny a Denied group -- "child
  // overrides parent" in the design doc.
  function groupEffective(g: AssistantGroupPermission): AssistantMode {
    return (g.mode || g.default_mode) as AssistantMode
  }

  function toolEffective(g: AssistantGroupPermission, toolMode: AssistantToolMode): AssistantMode {
    return toolMode === 'inherit' ? groupEffective(g) : (toolMode as AssistantMode)
  }

  async function load() {
    loading = true
    error = ''
    try {
      const data = await api.getAssistantPermissions()
      groups = data.groups
      collapse = new CollapseController('assistant-security', groups.map((g) => g.key))
    } catch (e: any) {
      error = e.message
    } finally {
      loading = false
    }
  }

  function setGroupMode(g: AssistantGroupPermission, mode: string) {
    g.mode = mode as AssistantMode | ''
    groups = [...groups]
  }

  function setToolMode(g: AssistantGroupPermission, toolName: string, mode: string) {
    const tool = g.tools.find((t) => t.name === toolName)
    if (tool) tool.mode = mode as AssistantToolMode
    groups = [...groups]
  }

  function resetChildren(g: AssistantGroupPermission) {
    for (const t of g.tools) t.mode = 'inherit'
    groups = [...groups]
  }

  async function save() {
    error = ''
    saved = false
    const groupPayload: Record<string, AssistantMode> = {}
    const toolPayload: Record<string, AssistantToolMode> = {}
    for (const g of groups) {
      // Only what differs from the built-in default is stored. Persisting an
      // explicit copy of every default would silently pin the user to today's
      // defaults if a later release changed one.
      if (g.mode && g.mode !== g.default_mode) groupPayload[g.key] = g.mode as AssistantMode
      for (const t of g.tools) {
        if (t.mode && t.mode !== 'inherit') toolPayload[t.name] = t.mode
      }
    }
    try {
      const data = await api.updateAssistantPermissions({ groups: groupPayload, tools: toolPayload })
      groups = data.groups
      saved = true
      setTimeout(() => (saved = false), 2000)
    } catch (e: any) {
      error = e.message
    }
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Assistant Security <a class="help-link" href="/help/user-guide.html#assistant-security" target="_blank" title="Open documentation">?</a></h2>
  <div style="display: flex; gap: 4px;">
    <button onclick={() => collapse.setAll(true)}>Collapse All</button>
    <button onclick={() => collapse.setAll(false)}>Expand All</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if saved}<div class="success-msg">Permissions saved.</div>{/if}

<p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
  Deny is absolute: nothing overrides it once it is the effective mode for a tool. Yolo lives in
  the Assistant pane, not here — it only ever collapses Normal's asks, and only for tools in
  <strong>content</strong> groups. Configuration and admin tools always ask under Normal, yolo or not.
</p>

{#if loading}
  <div class="loading">Loading...</div>
{:else}
  <button class="primary" onclick={save} style="margin-bottom: 16px;">Save</button>

  {#each groups as g (g.key)}
    <CollapsibleCard title={g.label} cardKey={g.key} {collapse}>
      <div class="card-header" style="align-items: flex-start; flex-wrap: wrap; gap: 12px;">
        <div>
          <a href={`#${g.page}`} target="_blank" rel="noopener" style="font-size: 12px;">{g.page}</a>
          <span class="badge" style="margin-left: 8px; text-transform: capitalize;">{g.category}</span>
          {#if g.disabled_by_admin}
            <span class="badge violation" style="margin-left: 8px;">Disabled by admin</span>
          {/if}
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <select
            value={g.mode || ''}
            disabled={g.disabled_by_admin}
            onchange={(e) => setGroupMode(g, (e.currentTarget as HTMLSelectElement).value)}
          >
            <option value="">Default ({modeLabel(g.default_mode)})</option>
            {#each MODES as m}<option value={m.value}>{m.label}</option>{/each}
          </select>
          <button onclick={() => resetChildren(g)} disabled={g.disabled_by_admin}>Reset children to Inherit</button>
        </div>
      </div>

      {#if g.disabled_by_admin}
        <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px;">
          This group is turned off for every user. An admin can turn it on in Admin &rarr; Global Caps.
        </p>
      {:else if g.tools.length === 0}
        <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px;">No tools in this group.</p>
      {:else}
        <div style="overflow-x: auto;">
          <table style="margin-top: 12px;">
            <thead>
              <tr><th>Tool</th><th>Summary</th><th>Risk</th><th>Mode</th><th>Effective</th></tr>
            </thead>
            <tbody>
              {#each g.tools as t (t.name)}
                <tr>
                  <td style="font-family: var(--mono); font-size: 12px;">{t.name}</td>
                  <td style="font-size: 12px; color: var(--text-dim);">{t.summary}</td>
                  <td><span class={`risk-badge risk-${t.risk}`}>{modeLabel(t.risk)}</span></td>
                  <td>
                    <select
                      value={t.mode}
                      onchange={(e) => setToolMode(g, t.name, (e.currentTarget as HTMLSelectElement).value)}
                    >
                      {#each TOOL_MODES as m}<option value={m.value}>{m.label}</option>{/each}
                    </select>
                  </td>
                  <td><span class={`mode-badge mode-${toolEffective(g, t.mode)}`}>{modeLabel(toolEffective(g, t.mode))}</span></td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </CollapsibleCard>
  {/each}
{/if}
