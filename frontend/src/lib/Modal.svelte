<script lang="ts">
  import type { Snippet } from 'svelte'

  let {
    open = true,
    title = '',
    width = '',
    onclose,
    children,
  }: {
    open?: boolean
    title?: string
    width?: string
    onclose?: () => void
    children: Snippet
  } = $props()
</script>

<svelte:window
  onkeydown={(e) => {
    if (open && e.key === 'Escape') onclose?.()
  }}
/>

{#if open}
  <div
    class="modal-overlay"
    role="dialog"
    aria-modal="true"
    tabindex="-1"
    onclick={(e) => {
      if (e.target === e.currentTarget) onclose?.()
    }}
    onkeydown={(e) => {
      if (e.key === 'Escape') onclose?.()
    }}
  >
    <div class="modal" style={width ? `width: ${width};` : undefined}>
      {#if title}<h3>{title}</h3>{/if}
      {@render children()}
    </div>
  </div>
{/if}
