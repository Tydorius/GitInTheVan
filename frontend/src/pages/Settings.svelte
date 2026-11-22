<script lang="ts">
  import { api } from '../api'
  import { onMount } from 'svelte'
  import CollapsibleCard from '../lib/CollapsibleCard.svelte'
  import ParameterEditor from '../lib/ParameterEditor.svelte'
  import ModelSelect from '../lib/ModelSelect.svelte'
  import { CollapseController } from '../lib/collapse'
  import { loadConfig as loadAssistantConfig } from '../lib/assistant/store'

  let settings = {
    default_endpoint_id: '' as string | null,
    preserve_thinking: true,
    gitv_status: false,
    simulated_streaming_speed: 0,
    streaming_post_processing_enabled: true,
  }
  // One parameter set per LLM role. These are the blanket layer: broader than
  // the endpoint, narrower than whatever the client sent.
  let driverParams: any[] = []
  let verificationParams: any[] = []
  let summarizationParams: any[] = []

  function modelsFor(endpointId: string | null): string[] {
    const ep = endpoints.find((e: any) => e.id === endpointId)
    return (ep?.models || []).map((m: any) => m.name).filter(Boolean)
  }
  let summarization = {
    summarization_enabled: false,
    summarization_endpoint_id: '' as string | null,
    summarization_model: '',
    summarization_token_threshold: 8000,
    summarization_keep_recent: 6,
    summarization_block_size: 0,
    summarization_prompt: '',
  }
  // Assistant Pane (Phase 26): the user's own endpoint acting as an in-app
  // assistant. Separate from the driver/verification/summarization roles above
  // -- it lives under /api/assistant, never /api/settings, so the assistant's
  // own config route cannot be reached by anything that writes to `settings`.
  let assistantForm = { endpoint_id: '' as string | null, model: '', context_tokens: 64000 }
  let assistantParams: any[] = []
  let assistantEnabled = true
  let assistantSaved = false
  let assistantError = ''

  $: assistantEndpoint = endpoints.find((e: any) => e.id === assistantForm.endpoint_id)
  $: assistantNotToolUse = !!assistantForm.endpoint_id && assistantEndpoint && assistantEndpoint.role_tag !== 'tool_use'

  async function saveAssistant() {
    assistantError = ''; assistantSaved = false
    try {
      const updated = await api.updateAssistantConfig({
        endpoint_id: assistantForm.endpoint_id || null,
        model: assistantForm.model,
        parameters: assistantParams,
        context_tokens: assistantForm.context_tokens,
      })
      assistantForm = {
        endpoint_id: updated.endpoint_id || '',
        model: updated.model || '',
        context_tokens: updated.context_tokens || 64000,
      }
      assistantParams = structuredClone(updated.parameters || [])
      assistantEnabled = updated.enabled
      loadAssistantConfig()
      assistantSaved = true
    } catch (e: any) { assistantError = e.message }
  }

  let driverCallableTurns = 1
  let bypassMethod = 'none'
  let prefillEnabled = false
  let budgetPercent = 10.0
  let contextWindowOverride = 0
  let debugMode = false
  let defaultMapId: string = ''
  let maps: any[] = []
  let endpoints: any[] = []
  let error = ''
  let saved = false
  let sumSaved = false
  let lexSaved = false

  // Phase 29: the lexicon compiler and the user half of the doubly-gated
  // cantrip auto-install. Both ride the general settings endpoint.
  let lexiconForm = { lexicon_enabled: true, lexicon_endpoint_id: '' as string | null }
  let lexiconParams: any[] = []
  let cantripAutoInstall = false

  // Phase 17: Passenger scripted LLM calls from cantrips. The user half of
  // the rule-22 call budget lives here; passengerEffectiveCap is read-only
  // from the server so the clamp is visible next to the field.
  let passengerEnabled = true
  let passengerBudget = 0
  let stripPatternsText = ''
  let stripSaved = false

  // Pipeline ordering (0.41.0): the manual pre-forward order.
  type OrderItem = { type: 'cantrip' | 'lorebook'; id: string; name: string }
  let orderCantrips: any[] = []
  let orderLorebooks: any[] = []
  let orderItems: OrderItem[] = []
  let orderEnabled = false
  let orderSaved = false
  let orderDragIndex = -1

  function orderName(type: string, id: string): string {
    if (type === 'cantrip') {
      return orderCantrips.find((c: any) => c.id === id)?.name || '(deleted cantrip)'
    }
    return orderLorebooks.find((l: any) => l.id === id)?.name || '(deleted lorebook)'
  }

  function populateOrderDefaults() {
    // Lorebooks first, cantrips second -- today's default window order.
    orderItems = [
      ...orderLorebooks.filter((l: any) => l.is_active).map((l: any) => ({ type: 'lorebook' as const, id: l.id, name: l.name })),
      ...orderCantrips.filter((c: any) => c.is_active && c.run_pre_driver).map((c: any) => ({ type: 'cantrip' as const, id: c.id, name: c.name })),
    ]
  }

  function moveOrderItem(index: number, delta: number) {
    const target = index + delta
    if (target < 0 || target >= orderItems.length) return
    const items = [...orderItems]
    const [moved] = items.splice(index, 1)
    items.splice(target, 0, moved)
    orderItems = items
  }

  function dropOrderItem(target: number) {
    if (orderDragIndex < 0 || orderDragIndex === target) return
    const items = [...orderItems]
    const [moved] = items.splice(orderDragIndex, 1)
    items.splice(target, 0, moved)
    orderItems = items
    orderDragIndex = -1
  }

  function removeOrderItem(index: number) {
    orderItems = [...orderItems.slice(0, index), ...orderItems.slice(index + 1)]
  }

  async function saveOrder() {
    error = ''
    saved = false
    try {
      await api.updateSettings({
        pipeline_order: {
          enabled: orderEnabled,
          items: orderItems.map(({ type, id }) => ({ type, id })),
        },
      })
      orderSaved = true
      setTimeout(() => { orderSaved = false }, 2500)
    } catch (e: any) { error = e.message }
  }
  let passengerParams: any[] = []
  let passengerEffectiveCap = 0
  let passengerSaved = false

  let collapse = new CollapseController('settings', ['proxy', 'streaming', 'summarization', 'assistant', 'driver', 'prefill', 'budget', 'lexicon', 'passenger', 'strippatterns', 'order'])

  async function load() {
    try {
      const [s, e, sum, mapData, cList, lList]: any[] = await Promise.all([api.getSettings(), api.listEndpoints(), api.getSummarizationSettings(), api.listMaps(), api.listCantrips(), api.listLorebooks()])
      orderCantrips = cList.cantrips
      orderLorebooks = lList.lorebooks
      settings.default_endpoint_id = s.default_endpoint_id || ''
      settings.preserve_thinking = s.preserve_thinking
      settings.gitv_status = s.gitv_status
      settings.simulated_streaming_speed = s.simulated_streaming_speed || 0
      settings.streaming_post_processing_enabled = s.streaming_post_processing_enabled ?? true
      driverParams = structuredClone(s.parameters || [])
      verificationParams = structuredClone(s.verification_parameters || [])
      summarizationParams = structuredClone(s.summarization_parameters || [])
      lexiconForm.lexicon_enabled = s.lexicon_enabled ?? true
      lexiconForm.lexicon_endpoint_id = s.lexicon_endpoint_id || ''
      lexiconParams = structuredClone(s.lexicon_parameters || [])
      cantripAutoInstall = s.cantrip_auto_install_enabled ?? false
      passengerEnabled = s.passenger_calling_enabled ?? true
      passengerBudget = s.passenger_call_budget ?? 0
      stripPatternsText = (s.response_strip_patterns || []).join('\n')
      const plan = s.pipeline_order || {}
      orderEnabled = !!plan.enabled
      orderItems = (plan.items || []).map((it: any) => ({
        type: it.type, id: it.id, name: orderName(it.type, it.id),
      }))
      passengerParams = structuredClone(s.passenger_parameters || [])
      passengerEffectiveCap = s.passenger_effective_cap ?? 0
      endpoints = e.endpoints
      maps = mapData.maps
      summarization.summarization_enabled = sum.summarization_enabled
      summarization.summarization_endpoint_id = sum.summarization_endpoint_id || ''
      summarization.summarization_model = sum.summarization_model || ''
      summarization.summarization_token_threshold = sum.summarization_token_threshold || 8000
      summarization.summarization_keep_recent = sum.summarization_keep_recent ?? 6
      summarization.summarization_block_size = Number(sum.summarization_block_size) || 0
      summarization.summarization_prompt = sum.summarization_prompt || ''

      driverCallableTurns = (s as any).driver_callable_turns ?? 1
      bypassMethod = (s as any).bypass_method ?? 'none'
      prefillEnabled = (s as any).prefill_enabled ?? false
      budgetPercent = (s as any).context_budget_percent ?? 10.0
      contextWindowOverride = (s as any).context_window_override ?? 0
      debugMode = (s as any).debug_mode ?? false
      defaultMapId = (s as any).default_map_id || ''

      try {
        const ac = await api.getAssistantConfig()
        assistantForm = { endpoint_id: ac.endpoint_id || '', model: ac.model || '', context_tokens: ac.context_tokens || 64000 }
        assistantParams = structuredClone(ac.parameters || [])
        assistantEnabled = ac.enabled
      } catch {
        // Admin-disabled, or the assistant router is unavailable -- the card
        // still renders so the user can see why nothing loaded.
        assistantEnabled = false
      }

    } catch (e: any) { error = e.message }
    finally { }
  }

  async function save() {
    error = ''; saved = false
    try { await api.updateSettings({ ...settings, debug_mode: debugMode, parameters: driverParams, verification_parameters: verificationParams } as any); saved = true }
    catch (e: any) { error = e.message }
  }

  async function saveSummarization() {
    error = ''; sumSaved = false
    try {
      await api.updateSummarizationSettings({
        ...summarization,
        summarization_endpoint_id: summarization.summarization_endpoint_id || '',
        summarization_block_size: Number(summarization.summarization_block_size) || 0,
      })
      // Parameters live on user_settings rather than the summarization
      // settings row, so they save through the general settings endpoint --
      // but from this button, where the user edited them.
      await api.updateSettings({ summarization_parameters: summarizationParams } as any)
      sumSaved = true
    } catch (e: any) { error = e.message }
  }

  async function saveDriverCallable() {
    error = ''
    try { await api.updateSettings({ driver_callable_turns: driverCallableTurns } as any) }
    catch (e: any) { error = e.message }
  }

  async function saveLexicon() {
    error = ''; lexSaved = false
    try {
      await api.updateSettings({
        lexicon_enabled: lexiconForm.lexicon_enabled,
        lexicon_endpoint_id: lexiconForm.lexicon_endpoint_id || '',
        lexicon_parameters: lexiconParams,
        cantrip_auto_install_enabled: cantripAutoInstall,
      } as any)
      lexSaved = true
    } catch (e: any) { error = e.message }
  }

  async function saveStripPatterns() {
    error = ''; stripSaved = false
    try {
      await api.updateSettings({
        response_strip_patterns: (stripPatternsText || '')
          .split('\n')
          .map((l: string) => l.trim())
          .filter(Boolean),
      } as any)
      stripSaved = true
    } catch (e: any) { error = e.message }
  }

  async function savePassenger() {
    error = ''; passengerSaved = false
    try {
      const s = await api.updateSettings({
        passenger_calling_enabled: passengerEnabled,
        passenger_call_budget: passengerBudget,
        passenger_parameters: passengerParams,
      } as any)
      passengerEffectiveCap = (s as any).passenger_effective_cap ?? passengerEffectiveCap
      passengerSaved = true
    } catch (e: any) { error = e.message }
  }

  async function saveBypassPrefill() {
    error = ''
    try {
      await api.updateSettings({ bypass_method: bypassMethod, prefill_enabled: prefillEnabled } as any)
    } catch (e: any) { error = e.message }
  }

  async function saveBudget() {
    error = ''
    try {
      const updated = await api.updateSettings({
        context_budget_percent: budgetPercent,
        context_window_override: contextWindowOverride,
        default_map_id: defaultMapId || null,
      } as any)
      defaultMapId = (updated as any).default_map_id || ''
    } catch (e: any) { error = e.message }
  }

  onMount(load)
</script>

<div class="page-header">
  <h2>Settings <a class="help-link" href="/help/user-guide.html#settings" target="_blank" title="Open documentation">?</a></h2>
  <div style="display: flex; gap: 4px;">
    <button onclick={() => collapse.setAll(true)}>Collapse All</button>
    <button onclick={() => collapse.setAll(false)}>Expand All</button>
  </div>
</div>

{#if error}<div class="error-msg">{error}</div>{/if}
{#if saved}<div class="success-msg">Settings saved.</div>{/if}

<CollapsibleCard title="Proxy Configuration" cardKey="proxy" {collapse}>
  <div class="form-group">
    <label for="default-ep">Default Endpoint</label>
    <select id="default-ep" bind:value={settings.default_endpoint_id}>
      <option value="">None</option>
      {#each endpoints as ep}<option value={ep.id}>{ep.name}</option>{/each}
    </select>
  </div>
  <div class="form-group">
    <ParameterEditor
      bind:params={driverParams}
      title="Default Parameters (all requests)"
      hint="Sent on every request unless an endpoint, model, rule or map stage sets the same name. These override whatever the client sent."
      scopeNote="Whatever the client sends is passed through untouched."
    />
  </div>
  <div class="form-group">
    <ParameterEditor
      bind:params={verificationParams}
      title="Default Parameters (verification calls)"
      hint="Sent on verification judge calls instead of the defaults above."
    />
  </div>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={debugMode} style="width: auto;">
      Debug Mode
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Captures pipeline stage data for the last 20 exchanges. View in the Debug tab under Dashboard.
    </p>
  </div>
  <button class="primary" onclick={save}>Save Settings</button>
</CollapsibleCard>

<CollapsibleCard title="Streaming and Status" cardKey="streaming" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    These settings affect how responses are delivered when verification is enabled. Since verification requires buffering the full response, these options let you control the streaming experience.
  </p>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={settings.gitv_status} style="width: auto;">
      GITV Status Block
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Includes a &lt;think&gt;&lt;gitv&gt; status block before the response showing pipeline activity (verification results, etc).
    </p>
  </div>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={settings.preserve_thinking} style="width: auto;">
      Preserve Thinking
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Include the LLM's reasoning/thought process in the final response if the model produces it.
    </p>
  </div>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={settings.streaming_post_processing_enabled} style="width: auto;">
      Run Response Checks on Streams
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Streaming responses are already buffered for memory extraction, so the forbidden-words scan and pre/post-Navigator cantrips can run at no extra buffering cost. Turn off to skip them and deliver the response the moment extraction finishes.
    </p>
  </div>
  <div class="form-group">
    <label for="stream-speed">Simulated Streaming Speed (tokens/min, 0 = instant)</label>
    <input id="stream-speed" type="number" bind:value={settings.simulated_streaming_speed} placeholder="0" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      When verification buffers the response, simulate streaming output at this speed. Set to 0 to deliver the entire response at once.
    </p>
  </div>
  <button class="primary" onclick={save}>Save Settings</button>
</CollapsibleCard>

<CollapsibleCard title="Conversation Summarization" cardKey="summarization" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    Automatically compress older conversation history into a summary when the token count exceeds the threshold. The summary is injected as context so the model retains long-running conversation details without filling the context window. Triggered before forwarding; works for both streaming and non-streaming requests.
  </p>
  {#if sumSaved}<div class="success-msg">Summarization settings saved.</div>{/if}
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={summarization.summarization_enabled} style="width: auto;">
      Enable Summarization
    </label>
  </div>
  <div class="form-group">
    <label for="sum-ep">Summarization Endpoint</label>
    <select id="sum-ep" bind:value={summarization.summarization_endpoint_id}>
      <option value="">Use default endpoint</option>
      {#each endpoints as ep}<option value={ep.id}>{ep.name}</option>{/each}
    </select>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Endpoint used to generate summaries. Can be a cheaper/faster model than the main endpoint.
    </p>
  </div>
  <div class="form-group">
    <label for="sum-model">Summarization Model Override</label>
    <ModelSelect
      id="sum-model"
      bind:value={summarization.summarization_model}
      models={modelsFor(summarization.summarization_endpoint_id)}
      emptyLabel="Use endpoint default"
      placeholder="Leave blank to use endpoint default"
    />
  </div>
  <div class="form-group">
    <ParameterEditor
      bind:params={summarizationParams}
      title="Default Parameters (summarization calls)"
      hint="Sent on conversation and scenario summarization calls, instead of the default parameters."
    />
  </div>
  <div class="form-group">
    <label for="sum-threshold">Token Threshold</label>
    <input id="sum-threshold" type="number" min="1" bind:value={summarization.summarization_token_threshold} placeholder="8000" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      When the estimated token count of the conversation exceeds this value, older messages are summarized. Token count is estimated at roughly 4 characters per token.
    </p>
  </div>
  <div class="form-group">
    <label for="sum-keep">Keep Recent Messages</label>
    <input id="sum-keep" type="number" min="3" bind:value={summarization.summarization_keep_recent} placeholder="6" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Number of recent messages always sent verbatim (never summarized). Minimum 3 to ensure a clean separation between the summary and current context. The rest are compressed into a summary.
    </p>
  </div>
  <div class="form-group">
    <label for="sum-block-size">Block Size</label>
    <input id="sum-block-size" type="number" min="0" bind:value={summarization.summarization_block_size} placeholder="0" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      0 = compact every turn; N &gt; 1 compacts in fixed blocks of N messages to keep prompt caches warm.
    </p>
  </div>
  <div class="form-group">
    <label for="sum-prompt">Summarization Prompt</label>
    <textarea id="sum-prompt" bind:value={summarization.summarization_prompt} rows="4" placeholder="Custom instructions for the summarization model. Leave blank to use the default."></textarea>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      System prompt sent to the summarization model. If cleared on save, the default prompt is restored on reload.
    </p>
  </div>
  <button class="primary" onclick={saveSummarization}>Save Summarization Settings</button>
</CollapsibleCard>

<CollapsibleCard title="Chat Lexicon & Cantrip Drafts" cardKey="lexicon" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    Two in-chat commands. <code>&lt;LEXICON:name&gt;</code> compiles a lorebook entry for a named
    thing (character, item, place) from the conversation via one side LLM call and files it in a
    per-chat lorebook that injects in this conversation only. <code>&lt;CANTRIP:description&gt;</code>
    queues a cantrip draft for the Assistant pane, where it is drafted, validated and created with
    the normal permission flow.
  </p>
  {#if lexSaved}<div class="success-msg">Lexicon settings saved.</div>{/if}
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={lexiconForm.lexicon_enabled} style="width: auto;">
      Lexicon Compiler Enabled
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Typing the command is the opt-in, so this defaults to on. Turn off to make
      <code>&lt;LEXICON:...&gt;</code> a no-op.
    </p>
  </div>
  <div class="form-group">
    <label for="lex-ep">Lexicon Endpoint</label>
    <select id="lex-ep" bind:value={lexiconForm.lexicon_endpoint_id}>
      <option value="">Use summarizer endpoint, then default</option>
      {#each endpoints as ep}<option value={ep.id}>{ep.name}</option>{/each}
    </select>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Used for the compile call. Falls back to the summarization endpoint (same long-context
      shape), then the default endpoint, then the first enabled endpoint.
    </p>
  </div>
  <div class="form-group">
    <ParameterEditor
      bind:params={lexiconParams}
      title="Default Parameters (lexicon calls)"
      hint="Sent on lexicon compile and cantrip auto-install drafting calls."
    />
  </div>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={cantripAutoInstall} style="width: auto;">
      Auto-Install Cantrip Drafts
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      With this on (and only if an admin has allowed it under Admin &rarr; Global Caps),
      <code>&lt;CANTRIP:...&gt;</code> drafts, validates, smoke-tests and installs the cantrip
      in-request instead of queueing it. A draft that fails validation is refused; one that
      fails only its smoke test is installed disabled. Both settings default off.
    </p>
  </div>
  <button class="primary" onclick={saveLexicon}>Save Lexicon Settings</button>
</CollapsibleCard>

<CollapsibleCard title="Response Cleanup (Strip Patterns)" cardKey="strippatterns" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    Regular expressions, one per line, stripped from every model reply before it reaches
    the client — site footers, watermark lines, artifacts. Applied at the same point as
    <a class="help-link" href="/help/user-guide.html#cantrips" target="_blank" title="Open documentation">response tags</a>;
    nothing is captured or stored. Max 20 patterns, 256 characters each.
  </p>
  {#if stripSaved}<div class="success-msg">Strip patterns saved.</div>{/if}
  <div class="form-group">
    <label for="strip-patterns">Patterns (one per line)</label>
    <textarea id="strip-patterns" rows="4" bind:value={stripPatternsText}
      placeholder="SITE_FOOTER_.*&#10;^\[watermark\].*$" style="width: 100%; font-family: monospace;"></textarea>
  </div>
  <button class="primary" onclick={saveStripPatterns}>Save Strip Patterns</button>
</CollapsibleCard>

<CollapsibleCard title="Pre-Forward Order" cardKey="order" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    By default the pipeline runs all lorebook injections, then all pre-Driver cantrips.
    Turn this on to drag <strong>cantrips and lorebooks into one exact interleaved order</strong> —
    Cantrip 1, Lorebook 1, Lorebook 2, Cantrip 2, anything. Each step's outputs (strips,
    injections, activations) land before the next step runs, so a lorebook matches the
    text an earlier cantrip stripped or wrote. Apply to non-map requests; maps keep their
    own stage order.
  </p>
  {#if orderSaved}<div class="success-msg">Pipeline order saved.</div>{/if}
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={orderEnabled} style="width: auto;">
      Manual Ordering Enabled
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Off (or an empty list) keeps today's batched behavior exactly. The list orders, it
      never activates: a resource still needs its tag or Active flag to fire.
    </p>
  </div>
  {#if orderEnabled && orderItems.length === 0}
    <div class="form-group">
      <button onclick={populateOrderDefaults}>Populate With Current Resources</button>
      <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
        Starts the list from every active lorebook and pre-Driver cantrip in today's order;
        then drag to reorder.
      </p>
    </div>
  {/if}
  {#if orderItems.length > 0}
    <div class="form-group">
      <label>Order (drag, or use the arrows)</label>
      <div style="display: grid; gap: 4px;">
        {#each orderItems as item, index (item.type + ':' + item.id)}
          <div
            draggable="true"
            ondragstart={() => { orderDragIndex = index }}
            ondragover={(e) => { e.preventDefault(); }}
            ondrop={(e) => { e.preventDefault(); dropOrderItem(index); }}
            ondragend={() => { orderDragIndex = -1 }}
            style="display: flex; justify-content: space-between; align-items: center; gap: 8px; border: 1px solid var(--border); border-radius: 4px; padding: 6px 8px; cursor: grab; background: var(--bg2, transparent);"
          >
            <div style="display: flex; align-items: center; gap: 8px; min-width: 0;">
              <span style="color: var(--text-dim); font-size: 11px; width: 18px; text-align: right;">{index + 1}</span>
              <span class="badge {item.type === 'cantrip' ? 'active' : ''}" style="font-size: 10px; flex-shrink: 0;">{item.type === 'cantrip' ? 'Cantrip' : 'Lorebook'}</span>
              <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{item.name}</span>
            </div>
            <div style="display: flex; gap: 2px; flex-shrink: 0;">
              <button style="padding: 2px 6px; font-size: 12px;" onclick={() => moveOrderItem(index, -1)} disabled={index === 0} title="Move up">↑</button>
              <button style="padding: 2px 6px; font-size: 12px;" onclick={() => moveOrderItem(index, 1)} disabled={index === orderItems.length - 1} title="Move down">↓</button>
              <button class="danger" style="padding: 2px 6px; font-size: 12px;" onclick={() => removeOrderItem(index)} title="Remove from list">✕</button>
            </div>
          </div>
        {/each}
      </div>
      <p style="color: var(--text-dim); font-size: 11px; margin-top: 6px;">
        Resources that activate but are not listed (including newly created ones) run after
        this list in their default order — nothing is silently dropped. A lorebook's
        "inject after cantrips" setting is ignored in manual mode; its position here is the
        truth.
      </p>
    </div>
  {/if}
  <button class="primary" onclick={saveOrder} disabled={orderEnabled && orderItems.length === 0}>
    Save Pipeline Order
  </button>
</CollapsibleCard>

<CollapsibleCard title="Passenger (Scripted LLM Calls)" cardKey="passenger" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    Lets cantrips make side LLM calls with <code>await context.passenger.call(...)</code> —
    the only network egress a sandboxed script ever gets. Calls go through your own
    endpoints with failover, are budgeted per request, and show in Debug and Metrics as
    Passenger calls. See the
    <a class="help-link" href="/help/user-guide.html#passenger" target="_blank" title="Open documentation">?</a>
    guide for the call API.
  </p>
  {#if passengerSaved}<div class="success-msg">Passenger settings saved.</div>{/if}
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={passengerEnabled} style="width: auto;">
      Passenger Calling Enabled
    </label>
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Installing a cantrip is the opt-in, so this defaults to on. When off (here or server-wide),
      <code>context.passenger.enabled</code> is false and <code>call()</code> returns null —
      scripts are expected to branch on that.
    </p>
  </div>
  <div class="form-group">
    <label for="psg-budget">Call Budget Per Request</label>
    <input id="psg-budget" type="number" min="0" bind:value={passengerBudget} style="width: 140px;" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Your own cap on Passenger calls per request, shared by every cantrip and map stage in
      that request. 0 means no self-imposed limit. Effective cap now:
      <strong>{passengerEffectiveCap} call(s)</strong> per request (the lower of this budget
      and the server's <code>max_passenger_calls_per_request</code>).
    </p>
  </div>
  <div class="form-group">
    <ParameterEditor
      bind:params={passengerParams}
      title="Default Parameters (passenger calls)"
      hint="Merged under endpoint and per-model parameters on every Passenger call."
    />
  </div>
  <button class="primary" onclick={savePassenger}>Save Passenger Settings</button>
</CollapsibleCard>

<CollapsibleCard title="Assistant" cardKey="assistant" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    Which of your own endpoints powers the Assistant Pane, and the model and parameters it uses.
    Permissions for what it may do are configured separately on the
    <a href="#/assistant-security">Assistant Security</a> page. See the
    <a class="help-link" href="/help/user-guide.html#assistant" target="_blank" title="Open documentation">?</a>
    guide for how the pane, its permission modes and its soft limits work.
  </p>
  {#if !assistantEnabled}
    <div class="warn-msg">The assistant is turned off for this server. An admin can enable it in Admin &rarr; Global Caps.</div>
  {/if}
  {#if assistantError}<div class="error-msg">{assistantError}</div>{/if}
  {#if assistantSaved}<div class="success-msg">Assistant settings saved.</div>{/if}
  <div class="form-group">
    <label for="assistant-ep">Assistant Endpoint</label>
    <select id="assistant-ep" bind:value={assistantForm.endpoint_id}>
      <option value="">None</option>
      {#each endpoints as ep}
        <option value={ep.id}>{ep.name}{ep.role_tag === 'tool_use' ? ' (tool_use)' : ''}</option>
      {/each}
    </select>
    {#if assistantNotToolUse}
      <p style="color: var(--warn); font-size: 11px; margin-top: 4px;">
        This endpoint is not tagged <code>tool_use</code>. It will still be offered to the model, but
        native tool calling may not work well if the tag is more than just missing.
      </p>
    {/if}
  </div>
  <div class="form-group">
    <label for="assistant-model">Model</label>
    <ModelSelect
      id="assistant-model"
      bind:value={assistantForm.model}
      models={modelsFor(assistantForm.endpoint_id)}
      emptyLabel="Use endpoint default"
      placeholder="Leave blank to use endpoint default"
    />
  </div>
  <div class="form-group">
    <label for="assistant-context">Working Context (tokens)</label>
    <input id="assistant-context" type="number" min="4000" step="1000" bind:value={assistantForm.context_tokens} style="width: 140px;" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Above 80% of this, the pane automatically compacts older tool results and, if still over,
      rolls the older half of the conversation into a summary. Set this to comfortably under the
      model's real context window.
    </p>
  </div>
  <div class="form-group">
    <ParameterEditor
      bind:params={assistantParams}
      title="Assistant Parameters"
      hint="Sent on assistant calls only. Does not affect the Driver, verification or summarization."
      scopeNote="No assistant-specific parameters are set."
    />
  </div>
  <button class="primary" onclick={saveAssistant}>Save Assistant Settings</button>
</CollapsibleCard>

<CollapsibleCard title="Driver-Callable Tools" cardKey="driver" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    When cantrips or lorebooks with the Driver-Callable position are active, the writing LLM can invoke them as tools via call tags. This setting controls the maximum number of tool-call rounds per request. Set to 0 to disable tool access entirely.
  </p>
  <div class="form-group">
    <label for="dc-turns">Driver-Callable Turns (0 = disabled)</label>
    <input id="dc-turns" type="number" min="0" bind:value={driverCallableTurns} placeholder="1" />
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">
      Auto-disables when no Driver-Callable resources are active for a request, regardless of this setting.
    </p>
  </div>
  <button class="primary" onclick={saveDriverCallable}>Save</button>
</CollapsibleCard>

<CollapsibleCard title="Prefill Normalization" cardKey="prefill" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    When a trailing assistant message is detected (prefill pattern), convert it to a system instruction for OpenAI-compatible providers that don't support native prefill. Anthropic and Google endpoints pass through as-is.
  </p>
  <div class="form-group">
    <label>
      <input type="checkbox" bind:checked={prefillEnabled} style="width: auto;">
      Enable Prefill Normalization
    </label>
  </div>
  <button class="primary" onclick={saveBypassPrefill}>Save</button>
</CollapsibleCard>

<CollapsibleCard title="Context Budgeting" cardKey="budget" {collapse}>
  <p style="color: var(--text-dim); font-size: 12px; margin-top: 12px; margin-bottom: 16px;">
    Allocates a percentage of the context window for injected content (cantrips, lorebooks, memory). Cantrips can access their allocation via <code>context.budget</code> to dynamically scale their output (full / summary / bullets). Set to 0 to disable.
  </p>
  <div class="form-group">
    <label for="budget-percent">Injection Budget (%)</label>
    <input id="budget-percent" type="number" bind:value={budgetPercent} min="0" max="100" step="0.5" style="width: 100px;">
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">Percentage of the context window reserved for cantrips and lorebooks.</p>
  </div>
  <div class="form-group">
    <label for="ctx-window">Context Window Override (tokens)</label>
    <input id="ctx-window" type="number" bind:value={contextWindowOverride} min="0" step="1000" style="width: 120px;">
    <p style="color: var(--text-dim); font-size: 11px; margin-top: 4px;">Override the model's context window size. 0 = auto-detect from model name.</p>
  </div>
  <div class="form-group">
    <label for="default-map">Default Map (applied when no <code>&lt;#map-tag#&gt;</code> is present)</label>
    <select id="default-map" bind:value={defaultMapId}>
      <option value="">No default map (standard pipeline)</option>
      {#each maps as m}<option value={m.id}>{m.name} ({m.stage_count} stages)</option>{/each}
    </select>
  </div>
  <button class="primary" onclick={saveBudget}>Save</button>
</CollapsibleCard>
