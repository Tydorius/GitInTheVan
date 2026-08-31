<script lang="ts">
  // Editor for one scope's LLM parameter list.
  //
  // Used identically wherever a model can be named -- endpoint, per-model,
  // verification rule, map stage, scenario rule, user settings -- because the
  // stored shape is the same at every scope and the closest one wins at request
  // time.
  //
  // Written in the codebase's Svelte-4 style, not runes: a nested array must be
  // reassigned after mutation or nothing re-renders (see Maps.svelte).

  export let params: any[] = []
  export let title: string = 'Parameters'
  export let hint: string = ''
  export let scopeNote: string = ''

  const TYPES = [
    { value: 'string', label: 'String' },
    { value: 'string[]', label: 'String list' },
    { value: 'integer', label: 'Integer' },
    { value: 'float', label: 'Float' },
    { value: 'number', label: 'Number (same as float)' },
    { value: 'boolean', label: 'Boolean' },
  ]

  const RESERVED = ['messages', 'model', 'stream']
  const NAME_RE = /^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/
  const MAX_PARAMS = 32

  function addParam() {
    params = [
      ...params,
      { name: '', type: 'string', value: '', description: '', required: false, options: [] },
    ]
  }

  function removeParam(idx: number) {
    params = params.filter((_, i) => i !== idx)
  }

  function optionsText(p: any): string {
    return Array.isArray(p.options) ? p.options.join(', ') : ''
  }

  function setOptions(idx: number, text: string) {
    params[idx].options = text.split(',').map((o) => o.trim()).filter(Boolean)
    params = [...params]
  }

  function listText(p: any): string {
    return Array.isArray(p.value) ? p.value.join(', ') : (p.value ?? '')
  }

  function setListValue(idx: number, text: string) {
    params[idx].value = text.split(',').map((v) => v.trim()).filter(Boolean)
    params = [...params]
  }

  function onTypeChange(idx: number) {
    // Switching to or from a list changes what shape `value` holds; converting
    // rather than clearing keeps whatever the user already typed.
    const p = params[idx]
    if (p.type === 'string[]' && !Array.isArray(p.value)) {
      p.value = String(p.value ?? '').split(',').map((v: string) => v.trim()).filter(Boolean)
    } else if (p.type !== 'string[]' && Array.isArray(p.value)) {
      p.value = p.value.join(', ')
    }
    params = [...params]
  }

  function isBlank(v: any): boolean {
    if (v === null || v === undefined) return true
    if (typeof v === 'string') return v.trim() === ''
    if (Array.isArray(v)) return v.length === 0
    return false
  }

  // Mirrors the server-side validator so a mistake is visible before saving
  // rather than arriving as a 422.
  function errorFor(p: any, idx: number): string {
    const name = (p.name ?? '').trim()
    if (!name) return 'Name is required'
    if (!NAME_RE.test(name)) return 'Letters, digits, _ . - only; must not start with a digit'
    if (RESERVED.includes(name) || name.startsWith('_gitv')) return `"${name}" is reserved`
    if (params.some((o, i) => i !== idx && (o.name ?? '').trim() === name)) {
      return 'Duplicate name'
    }
    if (p.required && isBlank(p.value)) return 'Marked required, so it needs a value'
    const opts: string[] = Array.isArray(p.options) ? p.options : []
    if (opts.length && !isBlank(p.value) && !opts.includes(String(p.value))) {
      return `Value must be one of: ${opts.join(', ')}`
    }
    return ''
  }

  $: errors = params.map((p, i) => errorFor(p, i))
  $: hasErrors = errors.some((e) => e)
</script>

<div class="param-editor">
  <div class="param-head">
    <label>{title}</label>
    {#if params.length < MAX_PARAMS}
      <button type="button" onclick={addParam}>+ Add Parameter</button>
    {:else}
      <span class="param-hint">Limit of {MAX_PARAMS} reached</span>
    {/if}
  </div>

  {#if hint}<p class="param-hint">{hint}</p>{/if}

  {#if params.length === 0}
    <p class="param-empty">
      None set.{scopeNote ? ` ${scopeNote}` : ''}
    </p>
  {/if}

  {#each params as p, idx}
    <div class="param-row" class:invalid={errors[idx]}>
      <div class="form-row">
        <div style="flex: 2;">
          <label for="p-name-{idx}">Name</label>
          <input id="p-name-{idx}" bind:value={p.name} placeholder="reasoning_effort" />
        </div>
        <div style="flex: 1.4;">
          <label for="p-type-{idx}">Type</label>
          <select id="p-type-{idx}" bind:value={p.type} onchange={() => onTypeChange(idx)}>
            {#each TYPES as t}<option value={t.value}>{t.label}</option>{/each}
          </select>
        </div>
        <div style="flex: 2;">
          <label for="p-value-{idx}">Value</label>
          {#if Array.isArray(p.options) && p.options.length}
            <select id="p-value-{idx}" bind:value={p.value}>
              <option value="">(none)</option>
              {#each p.options as o}<option value={o}>{o}</option>{/each}
            </select>
          {:else if p.type === 'boolean'}
            <select id="p-value-{idx}" bind:value={p.value}>
              <option value={true}>true</option>
              <option value={false}>false</option>
            </select>
          {:else if p.type === 'string[]'}
            <input
              id="p-value-{idx}"
              value={listText(p)}
              oninput={(e) => setListValue(idx, e.currentTarget.value)}
              placeholder="END, ###"
            />
          {:else}
            <input id="p-value-{idx}" bind:value={p.value} placeholder="max" />
          {/if}
        </div>
        <div style="flex: 0 0 90px; align-self: flex-end; padding-bottom: 8px;">
          <label style="display: flex; align-items: center; gap: 6px; margin: 0; cursor: pointer;">
            <input type="checkbox" bind:checked={p.required} style="width: auto;" />
            Required
          </label>
        </div>
        <div style="flex: 0 0 auto; align-self: flex-end;">
          <button type="button" class="danger" onclick={() => removeParam(idx)}>Remove</button>
        </div>
      </div>

      <div class="form-row" style="margin-top: 8px;">
        <div style="flex: 2;">
          <label for="p-opts-{idx}">Options (comma-separated, optional)</label>
          <input
            id="p-opts-{idx}"
            value={optionsText(p)}
            oninput={(e) => setOptions(idx, e.currentTarget.value)}
            placeholder="low, high, max"
          />
        </div>
        <div style="flex: 3;">
          <label for="p-desc-{idx}">Description (optional)</label>
          <input id="p-desc-{idx}" bind:value={p.description} placeholder="What this controls" />
        </div>
      </div>

      {#if errors[idx]}
        <p class="param-error">{errors[idx]}</p>
      {/if}
    </div>
  {/each}

  {#if hasErrors}
    <p class="param-error">Fix the problems above before saving.</p>
  {/if}
</div>

<style>
  .param-editor {
    margin-bottom: 16px;
  }
  .param-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 6px;
  }
  .param-head label {
    margin: 0;
  }
  .param-hint,
  .param-empty {
    color: var(--text-dim);
    font-size: 12px;
    margin: 0 0 8px 0;
  }
  .param-row {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px;
    margin-bottom: 8px;
    background: var(--bg);
  }
  .param-row.invalid {
    border-color: var(--danger);
  }
  .param-error {
    color: var(--danger);
    font-size: 12px;
    margin: 8px 0 0 0;
  }
</style>
