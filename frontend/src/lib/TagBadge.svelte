<script lang="ts">
  let {
    tag,
    onEdit,
  }: {
    tag: string
    onEdit?: () => void
  } = $props()

  let copied = $state(false)
  let timer: ReturnType<typeof setTimeout> | null = null

  function copyTag() {
    navigator.clipboard.writeText(tag)
    copied = true
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      copied = false
    }, 1500)
  }
</script>

<button
  type="button"
  class="api-key-display tag-badge"
  onclick={copyTag}
  title="Click to copy"
>
  {copied ? 'Copied!' : tag}
</button>
{#if onEdit}
  <button
    type="button"
    onclick={onEdit}
    style="padding: 0 4px; font-size: 14px; border: none; background: none; cursor: pointer;"
    title="Edit tag"
  >
    🖉
  </button>
{/if}

<style>
  .tag-badge {
    display: inline;
    font-size: 10px;
    padding: 2px 6px;
    cursor: pointer;
    margin: 0;
    color: inherit;
  }
</style>
