import { tick } from 'svelte'

const PENDING_KEY = '__gitv_pending_scroll'

/**
 * Captures the element with [data-scroll-anchor] closest to the top
 * of the viewport. Returns an object with the anchor ID and pixel
 * offset so scroll position can be restored after a re-render.
 */
function capture(): { id: string; offset: number } | null {
  const elements = document.querySelectorAll('[data-scroll-anchor]')
  if (!elements.length) return null

  let best: { id: string; offset: number } | null = null
  for (const el of elements) {
    const rect = el.getBoundingClientRect()
    const distance = Math.abs(rect.top)
    if (!best || distance < Math.abs(best.offset)) {
      best = { id: el.getAttribute('data-scroll-anchor') || '', offset: rect.top }
    }
  }
  return best
}

/**
 * Restores scroll position to a previously captured anchor.
 * Called after Svelte's DOM has updated (post-tick).
 */
function restore(anchor: { id: string; offset: number } | null) {
  if (!anchor) return
  const el = document.querySelector(`[data-scroll-anchor="${anchor.id}"]`)
  if (el) {
    const rect = el.getBoundingClientRect()
    window.scrollBy({ top: rect.top - anchor.offset, behavior: 'instant' as ScrollBehavior })
  }
}

/**
 * Wraps an async data-reload function with scroll preservation.
 * Captures scroll position before the reload, waits for the DOM
 * to update, then restores scroll to the same anchor.
 *
 * Usage: `await withScroll(load)` instead of `await load()`
 */
export async function withScroll<T>(fn: () => Promise<T>): Promise<T> {
  const anchor = capture()
  const result = await fn()
  await tick()
  restore(anchor)
  return result
}
