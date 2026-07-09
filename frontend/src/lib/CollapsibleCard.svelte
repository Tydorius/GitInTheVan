<script lang="ts">
  import type { Snippet } from 'svelte'
  import type { CollapseController } from './collapse'

  export let title: string = ''
  export let cardKey: string = ''
  export let collapse: CollapseController
  export let children: Snippet

  let isCollapsed = false

  $: if (collapse && cardKey) {
    collapse.store.subscribe(state => {
      isCollapsed = state[cardKey] ?? false
    })
  }

  function handleClick() {
    collapse.toggle(cardKey)
  }
</script>

<div class="card collapsible-card">
  <div
    class="card-collapse-header"
    onclick={handleClick}
    role="button"
    tabindex="0"
    onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleClick() } }}
  >
    {#if title}<h3>{title}</h3>{/if}
    <span class="card-chevron" style="margin-left: auto;">
      {isCollapsed ? '▶' : '▼'}
    </span>
  </div>
  {#if !isCollapsed}
    <div class="card-collapse-body">
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .collapsible-card {
    padding: 0;
    overflow: hidden;
  }
  .card-collapse-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 16px 20px;
    cursor: pointer;
    user-select: none;
  }
  .card-collapse-header:hover {
    background: var(--surface, rgba(255, 255, 255, 0.03));
  }
  .card-collapse-header h3 {
    margin: 0;
  }
  .card-chevron {
    color: var(--text-dim, #8b90a5);
    font-size: 12px;
    transition: transform 0.15s ease;
  }
  .card-collapse-body {
    padding: 0 20px 20px 20px;
  }
</style>
