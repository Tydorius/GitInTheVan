import { writable, type Writable } from 'svelte/store'

const PREFIX = 'gitv_collapsed'

export class CollapseController {
  page: string
  store: Writable<{ [key: string]: boolean }>

  constructor(page: string, cardIds: string[]) {
    this.page = page
    const initial: { [key: string]: boolean } = {}
    for (const id of cardIds) {
      try {
        const stored = localStorage.getItem(`${PREFIX}:${page}:${id}`)
        initial[id] = stored === null ? false : stored === '1'
      } catch {
        initial[id] = false
      }
    }
    this.store = writable(initial)
  }

  toggle(cardId: string) {
    this.store.update(state => {
      const next = !(state[cardId] ?? false)
      state[cardId] = next
      try {
        localStorage.setItem(`${PREFIX}:${this.page}:${cardId}`, next ? '1' : '0')
      } catch {}
      return { ...state }
    })
  }

  setAll(collapsed: boolean) {
    this.store.update(state => {
      for (const id of Object.keys(state)) {
        state[id] = collapsed
        try {
          localStorage.setItem(`${PREFIX}:${this.page}:${id}`, collapsed ? '1' : '0')
        } catch {}
      }
      return { ...state }
    })
  }
}
