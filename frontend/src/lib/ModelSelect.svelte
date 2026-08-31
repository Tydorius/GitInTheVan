<script lang="ts">
  // A model picker backed by an endpoint's curated model list, with a free-text
  // escape hatch.
  //
  // Every model field in this app was free text before Phase 23, so existing
  // configs hold names that are not in any list. Those must keep working and
  // must remain editable, which is why "Other" is never removed and why a value
  // that is not in the list automatically opens the text input.

  export let value: string = ''
  export let models: string[] = []
  export let placeholder: string = 'Model name'
  export let allowEmpty: boolean = true
  export let emptyLabel: string = 'Use default'
  export let id: string = ''

  const OTHER = '__other__'

  let mode: 'list' | 'other' = 'list'
  let lastSyncedValue: string | null = null

  // Re-derive the mode when the bound value or the list changes from outside
  // (loading a record, switching endpoint), but not on every keystroke the user
  // makes in the free-text box -- which is why the sync is guarded on value
  // actually having changed since we last looked.
  $: if (value !== lastSyncedValue) {
    lastSyncedValue = value
    mode = value && !models.includes(value) ? 'other' : 'list'
  }

  function onSelect(e: Event) {
    const picked = (e.target as HTMLSelectElement).value
    if (picked === OTHER) {
      mode = 'other'
      lastSyncedValue = value
    } else {
      mode = 'list'
      value = picked
      lastSyncedValue = picked
    }
  }
</script>

{#if mode === 'other'}
  <div style="display: flex; gap: 8px;">
    <input {id} bind:value {placeholder} />
    {#if models.length}
      <button
        type="button"
        style="flex: 0 0 auto;"
        onclick={() => { mode = 'list'; value = ''; lastSyncedValue = '' }}
      >
        Choose
      </button>
    {/if}
  </div>
{:else}
  <select {id} value={models.includes(value) ? value : ''} onchange={onSelect}>
    {#if allowEmpty}<option value="">{emptyLabel}</option>{/if}
    {#each models as m}<option value={m}>{m}</option>{/each}
    <option value={OTHER}>Other…</option>
  </select>
{/if}
