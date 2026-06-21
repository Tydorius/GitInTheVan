<script lang="ts">
  export let show: boolean = false
  export let resourceType: string = 'cantrip'
  export let resourceName: string = ''
  export let currentTag: string = ''
  export let errorMsg: string = ''
  export let onSave: (tag: string) => void

  let editTag: string = ''

  $: if (show) {
    editTag = currentTag || ''
  }

  function handleSave() {
    onSave(editTag.trim())
  }

  function fullTag() {
    if (!editTag.trim()) return ''
    return '<#' + resourceType + '-' + editTag.trim() + '#>'
  }
</script>

{#if show}
  <div class="modal-overlay" role="dialog" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) { show = false; errorMsg = ''; } }}>
    <div class="modal" style="width: 450px;">
      <h3>Edit Tag</h3>
      <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
        Set a tag for <strong>{resourceName}</strong>. Users activate this by including the tag in their persona or messages.
      </p>
      {#if errorMsg}<div class="error-msg" style="margin-bottom: 12px;">{errorMsg}</div>{/if}
      <div class="form-group">
        <label for="tag-edit-input">Tag Name</label>
        <div style="display: flex; align-items: center; gap: 4px;">
          <span style="color: var(--text-dim); font-family: var(--mono); font-size: 13px;">&lt;#{resourceType}-</span>
          <input
            id="tag-edit-input"
            autocomplete="off"
            spellcheck="false"
            bind:value={editTag}
            oninput={() => errorMsg = ''}
            placeholder="my-tag"
            style="flex: 1;"
          />
          <span style="color: var(--text-dim); font-family: var(--mono); font-size: 13px;">#&gt;</span>
        </div>
      </div>
      {#if editTag.trim()}
        <div style="margin: 12px 0;">
          <span style="font-size: 11px; color: var(--text-dim);">Full activation tag:</span>
          <div class="api-key-display" style="cursor: pointer; margin-top: 4px;"
               onclick={() => navigator.clipboard.writeText(fullTag())}
               title="Click to copy">
            {fullTag()}
          </div>
        </div>
      {/if}
      <div class="modal-actions">
        <button onclick={() => { show = false; errorMsg = ''; }}>Cancel</button>
        <button class="primary" onclick={handleSave}>Save</button>
      </div>
    </div>
  </div>
{/if}
