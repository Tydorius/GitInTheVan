<script lang="ts">
  /**
   * Link to an object elsewhere in the app, opening in a new tab.
   *
   * Debug and Compare use this to jump to a cantrip, lorebook entry, skill or
   * map stage that a run touched. New tab is deliberate: the point of the
   * comparison view is that you keep it open while you go and fix the thing it
   * showed you.
   *
   * Renders plain text when the type has no page or the id is missing, so a
   * stage recorded without identity does not become a link to nowhere.
   */
  import { resourceLink } from './deeplink'

  export let type: string
  export let id: string | null | undefined = ''
  export let label: string = ''
  export let extra: Record<string, string> = {}
  export let title: string = ''

  $: href = id ? resourceLink(type, id, extra) : null
  $: text = label || id || ''
</script>

{#if href}
  <a
    {href}
    target="_blank"
    rel="noopener"
    title={title || `Open ${text} in a new tab`}
    class="gitv-link"
  >{text}<span class="ext" aria-hidden="true">↗</span></a>
{:else}
  <span class="gitv-link-plain">{text}</span>
{/if}

<style>
  .gitv-link {
    color: var(--accent);
    text-decoration: none;
    border-bottom: 1px dotted var(--accent);
  }
  .gitv-link:hover {
    color: var(--accent-hover);
    border-bottom-style: solid;
  }
  .ext {
    font-size: 0.8em;
    margin-left: 3px;
    opacity: 0.7;
  }
  .gitv-link-plain {
    color: var(--text-dim);
  }
</style>
