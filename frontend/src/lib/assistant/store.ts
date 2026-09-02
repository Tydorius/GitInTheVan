/**
 * Assistant Pane state: pane UI (expanded/width, in localStorage), the
 * conversation picker list, and the single active conversation with its live
 * SSE-driven transcript.
 *
 * Imports `api`/`sse`, never the reverse -- components read these stores and
 * call the exported functions; nothing here touches the DOM.
 */

import { writable, get } from 'svelte/store'
import { api } from '../../api'
import type { AssistantConfig, AssistantConversationSummary } from '../../api'
import { postEventStream } from '../sse'
import { routeParams } from '../../stores'
import { buildRoute } from '../deeplink'
import { downloadFromApi } from '../download'

// ---------------------------------------------------------------------------
// Pane UI state
// ---------------------------------------------------------------------------

function readLocalBool(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key)
    return raw === null ? fallback : raw === '1'
  } catch {
    return fallback
  }
}

function writeLocal(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Private browsing / storage disabled -- the pane still works, it just
    // forgets its expanded state and width across reloads.
  }
}

function maxPaneWidth(): number {
  const vw = typeof window !== 'undefined' && window.innerWidth ? window.innerWidth : 1280
  return Math.max(320, Math.floor(vw / 2))
}

function clampPaneWidth(w: number): number {
  return Math.min(Math.max(w, 320), maxPaneWidth())
}

export const paneExpanded = writable<boolean>(readLocalBool('gitv_assistant_open', false))
paneExpanded.subscribe((v) => writeLocal('gitv_assistant_open', v ? '1' : '0'))

function readLocalWidth(): number {
  try {
    const raw = localStorage.getItem('gitv_assistant_width')
    const n = raw === null ? 380 : parseInt(raw, 10)
    return clampPaneWidth(Number.isFinite(n) ? n : 380)
  } catch {
    return 380
  }
}

export const paneWidth = writable<number>(readLocalWidth())

/** Set the pane width, clamped to [320, half the viewport]. */
export function setPaneWidth(width: number): void {
  const clamped = clampPaneWidth(width)
  paneWidth.set(clamped)
  writeLocal('gitv_assistant_width', String(clamped))
}

// ---------------------------------------------------------------------------
// Assistant availability / configuration
// ---------------------------------------------------------------------------

export const assistantConfig = writable<AssistantConfig | null>(null)
export const assistantEnabled = writable<boolean>(false)

/** Load the assistant's endpoint/model/permissions config. Fails closed:
 * an error (admin-disabled, no endpoint, network) just hides the pane rather
 * than breaking the rest of the app. */
export async function loadConfig(): Promise<void> {
  try {
    const cfg = await api.getAssistantConfig()
    assistantConfig.set(cfg)
    assistantEnabled.set(cfg.enabled)
  } catch {
    assistantConfig.set(null)
    assistantEnabled.set(false)
  }
}

// ---------------------------------------------------------------------------
// Conversation transcript projection
// ---------------------------------------------------------------------------

export type ChatItemKind = 'user' | 'assistant' | 'tool_call' | 'tool_result' | 'system_note' | 'compacted_divider'

/** One row in the pane's transcript, projected from the OpenAI-format
 * `messages` the API stores (see `AssistantConversation.messages`). */
export interface ChatItem {
  kind: ChatItemKind
  id: string
  content?: string
  reasoning_content?: string
  call_id?: string
  name?: string
  args?: any
  ok?: boolean
  result?: any
  truncated?: boolean
  duration_ms?: number
  risk?: string
}

export interface PendingConfirm {
  call_id: string
  name: string
  args: any
  risk: string
  summary: string
  current?: any
  group: string
  page: string
  can_allow_session: boolean
}

export interface ActiveUsage {
  prompt_tokens: number
  completion_tokens: number
  llm_calls: number
  tool_calls: number
}

export interface ActiveConversation {
  id: string
  title: string
  saved: boolean
  yolo: boolean
  items: ChatItem[]
  usage: ActiveUsage
  compactionIndex: number | null
}

function toolCallArgs(raw: any): any {
  const args = raw?.function?.arguments ?? raw?.args
  if (typeof args !== 'string') return args ?? {}
  try {
    return JSON.parse(args)
  } catch {
    return args
  }
}

function toolResultBody(content: any): any {
  if (typeof content !== 'string') return content
  try {
    return JSON.parse(content)
  } catch {
    return content
  }
}

/** Turn the stored OpenAI-format message list into transcript rows, with a
 * "Context compacted" divider inserted at the compaction boundary. */
function projectMessages(messages: any[], compaction: { summary: string; through_index: number } | null): ChatItem[] {
  const items: ChatItem[] = []
  const boundary = compaction ? compaction.through_index : -1

  messages.forEach((m, idx) => {
    if (idx === boundary) {
      items.push({ kind: 'compacted_divider', id: `divider-${idx}` })
    }

    if (m.role === 'user') {
      const content = typeof m.content === 'string' ? m.content : JSON.stringify(m.content)
      items.push({ kind: 'user', id: `msg-${idx}`, content })
    } else if (m.role === 'assistant') {
      if (m.content) {
        items.push({ kind: 'assistant', id: `msg-${idx}`, content: m.content, reasoning_content: m.reasoning_content })
      }
      for (const tc of m.tool_calls || []) {
        items.push({
          kind: 'tool_call',
          id: `call-${tc.id}`,
          call_id: tc.id,
          name: tc.function?.name ?? tc.name,
          args: toolCallArgs(tc),
        })
      }
    } else if (m.role === 'tool') {
      const result = toolResultBody(m.content)
      const isError = !!(result && typeof result === 'object' && result.error)
      items.push({
        kind: 'tool_result',
        id: `result-${m.tool_call_id ?? idx}`,
        call_id: m.tool_call_id,
        ok: !isError,
        result,
      })
    } else if (m.role === 'system' && idx > 0) {
      // A system-role message after the first is a mid-transcript note (a
      // rolled-forward compaction summary, or the seed note on a
      // "New from summary" fork) rather than the conversation's own prompt.
      const content = typeof m.content === 'string' ? m.content : JSON.stringify(m.content)
      items.push({ kind: 'system_note', id: `msg-${idx}`, content })
    }
  })

  return items
}

function toActive(conv: {
  id: string
  title: string
  saved: boolean
  yolo: boolean
  messages: any[]
  compaction: { summary: string; through_index: number } | null
  prompt_tokens: number
  completion_tokens: number
  llm_calls: number
  tool_calls: number
}): ActiveConversation {
  return {
    id: conv.id,
    title: conv.title,
    saved: conv.saved,
    yolo: conv.yolo,
    items: projectMessages(conv.messages || [], conv.compaction || null),
    usage: {
      prompt_tokens: conv.prompt_tokens || 0,
      completion_tokens: conv.completion_tokens || 0,
      llm_calls: conv.llm_calls || 0,
      tool_calls: conv.tool_calls || 0,
    },
    compactionIndex: conv.compaction ? conv.compaction.through_index : null,
  }
}

// ---------------------------------------------------------------------------
// Conversation list + active conversation
// ---------------------------------------------------------------------------

export const conversations = writable<AssistantConversationSummary[]>([])
export const active = writable<ActiveConversation | null>(null)
export const pending = writable<PendingConfirm | null>(null)
export const busy = writable<boolean>(false)
export const lastError = writable<string>('')
/** One-line notices: conversation rotation, non-tool_use endpoint, etc. */
export const notice = writable<string>('')
/** True after a `done{reason:"tool_cap"}` -- shows the Continue button. */
export const capHit = writable<boolean>(false)

let controller: AbortController | null = null

export async function refreshConversations(): Promise<void> {
  try {
    const data: any = await api.listAssistantConversations()
    conversations.set(data.conversations || [])
  } catch (e: any) {
    lastError.set(e?.message || 'Failed to load conversations')
  }
}

/** A reload-time `pending_json`. Confirmation state renders as a ConfirmCard;
 * a still-pending client action (e.g. reload mid-navigate) is just replayed. */
function applyServerPending(raw: any): void {
  if (!raw) {
    pending.set(null)
    return
  }
  if (raw.kind === 'client') {
    performClientAction(raw.call_id, raw.name === 'navigate' ? { navigate: raw.args } : {}).catch((e: any) => {
      lastError.set(e?.message || 'Failed to resume the assistant')
    })
    return
  }
  pending.set(raw as PendingConfirm)
}

export async function openConversation(id: string): Promise<void> {
  lastError.set('')
  notice.set('')
  capHit.set(false)
  pending.set(null)
  try {
    const conv = await api.getAssistantConversation(id)
    active.set(toActive(conv))
    applyServerPending(conv.pending)
    if (!conv.messages?.length) {
      const cfg = get(assistantConfig)
      if (cfg && cfg.endpoint_role_tag && cfg.endpoint_role_tag !== 'tool_use') {
        notice.set('This endpoint is not tagged tool_use; the model may have trouble using tools.')
      }
    }
  } catch (e: any) {
    lastError.set(e?.message || 'Failed to open conversation')
  }
}

export async function newConversation(): Promise<void> {
  lastError.set('')
  notice.set('')
  capHit.set(false)
  pending.set(null)
  try {
    const result = await api.createAssistantConversation()
    active.set({
      id: result.id,
      title: result.title,
      saved: result.saved,
      yolo: false,
      items: [],
      usage: { prompt_tokens: 0, completion_tokens: 0, llm_calls: 0, tool_calls: 0 },
      compactionIndex: null,
    })
    const cfg = get(assistantConfig)
    if (cfg && cfg.endpoint_role_tag && cfg.endpoint_role_tag !== 'tool_use') {
      notice.set('This endpoint is not tagged tool_use; the model may have trouble using tools.')
    } else if (result.rotated_title) {
      notice.set(`Oldest unsaved conversation "${result.rotated_title}" was removed to stay under the limit.`)
    }
    await refreshConversations()
  } catch (e: any) {
    lastError.set(e?.message || 'Failed to start a new conversation')
  }
}

// ---------------------------------------------------------------------------
// SSE event handling
// ---------------------------------------------------------------------------

function appendItem(item: ChatItem): void {
  const conv = get(active)
  if (!conv) return
  active.set({ ...conv, items: [...conv.items, item] })
}

export function handleEvent(name: string, data: any): void {
  const conv = get(active)

  switch (name) {
    case 'meta': {
      if (conv && data?.conversation_id && conv.id !== data.conversation_id) {
        active.set({ ...conv, id: data.conversation_id })
      }
      break
    }

    case 'assistant_message': {
      if (!conv) break
      const items = conv.items.slice()
      if (data.content) {
        items.push({
          kind: 'assistant',
          id: `live-asst-${data.index ?? items.length}`,
          content: data.content,
          reasoning_content: data.reasoning_content,
        })
      }
      for (const tc of data.tool_calls || []) {
        items.push({ kind: 'tool_call', id: `live-call-${tc.id}`, call_id: tc.id, name: tc.name, args: tc.args })
      }
      active.set({ ...conv, items })
      break
    }

    case 'tool_call': {
      // The loop always sends assistant_message with its tool_calls first;
      // this event is a redundant per-call signal. De-dupe by call_id so a
      // tool the pane already shows does not appear twice.
      if (!conv) break
      if (conv.items.some((i) => i.kind === 'tool_call' && i.call_id === data.call_id)) break
      appendItem({ kind: 'tool_call', id: `live-call-${data.call_id}`, call_id: data.call_id, name: data.name, args: data.args, risk: data.risk })
      break
    }

    case 'tool_result': {
      appendItem({
        kind: 'tool_result',
        id: `live-result-${data.call_id}`,
        call_id: data.call_id,
        ok: data.ok,
        result: data.result,
        truncated: data.truncated,
        duration_ms: data.duration_ms,
      })
      break
    }

    case 'awaiting_confirmation': {
      pending.set(data as PendingConfirm)
      busy.set(false)
      break
    }

    case 'client_action': {
      performClientAction(data.call_id, data.name === 'navigate' ? { navigate: data.args } : {}).catch((e: any) => {
        lastError.set(e?.message || 'Failed to perform the requested navigation')
      })
      break
    }

    case 'awaiting_client': {
      // No-op: the navigation already ran inside the `client_action` handler
      // above. Named explicitly so it is clear this event is intentionally
      // not ignored by accident.
      break
    }

    case 'usage': {
      if (!conv) break
      active.set({
        ...conv,
        usage: {
          prompt_tokens: data.total_prompt_tokens ?? conv.usage.prompt_tokens,
          completion_tokens: data.total_completion_tokens ?? conv.usage.completion_tokens,
          llm_calls: data.llm_calls ?? conv.usage.llm_calls,
          tool_calls: data.tool_calls ?? conv.usage.tool_calls,
        },
      })
      break
    }

    case 'error': {
      lastError.set(data?.message || data?.code || 'Assistant error')
      busy.set(false)
      break
    }

    case 'done': {
      busy.set(false)
      capHit.set(data?.reason === 'tool_cap')
      break
    }

    default:
      break
  }
}

/** Perform a `client_action` (currently only `navigate`), then report the
 * result back through `resume` with `client_result` so the loop continues. */
export async function performClientAction(callId: string, action: any): Promise<void> {
  const nav = action?.navigate
  let navigated = false

  if (nav?.page) {
    const target = buildRoute(nav.page, nav.params || {})
    if (window.location.hash !== target) {
      const wait = new Promise<void>((resolve) => {
        let settled = false
        const onChange = () => {
          if (settled) return
          settled = true
          window.removeEventListener('hashchange', onChange)
          resolve()
        }
        window.addEventListener('hashchange', onChange)
        setTimeout(onChange, 50)
      })
      window.location.hash = target
      await wait
    }
    navigated = true
  }

  const conv = get(active)
  if (!conv) return
  await streamResume(conv.id, { call_id: callId, client_result: { navigated, page: nav?.page } })
}

async function streamResume(conversationId: string, body: any): Promise<void> {
  busy.set(true)
  controller = new AbortController()
  try {
    await postEventStream(`/api/assistant/conversations/${conversationId}/resume`, body, handleEvent, controller.signal)
  } catch (e: any) {
    lastError.set(e?.message || 'Assistant request failed')
  } finally {
    busy.set(false)
    controller = null
  }
}

export async function decide(decision: 'approve' | 'allow_session' | 'reject', note?: string): Promise<void> {
  const p = get(pending)
  const conv = get(active)
  if (!p || !conv) return
  pending.set(null)
  await streamResume(conv.id, { call_id: p.call_id, decision, note: note || undefined })
}

export async function sendMessage(text: string): Promise<void> {
  const content = text.trim()
  if (!content) return

  let conv = get(active)
  if (!conv) {
    await newConversation()
    conv = get(active)
    if (!conv) return
  }

  active.set({ ...conv, items: [...conv.items, { kind: 'user', id: `local-${Date.now()}`, content }] })
  busy.set(true)
  controller = new AbortController()
  try {
    await postEventStream(
      `/api/assistant/conversations/${conv.id}/message`,
      { content, route: get(routeParams) },
      handleEvent,
      controller.signal,
    )
  } catch (e: any) {
    lastError.set(e?.message || 'Assistant request failed')
  } finally {
    busy.set(false)
    controller = null
  }
}

/** Sends the literal `/continue` marker: the backend starts a fresh turn
 * with a fresh per-turn tool-call counter. */
export async function continueTurn(): Promise<void> {
  capHit.set(false)
  await sendMessage('/continue')
}

export function abort(): void {
  controller?.abort()
  busy.set(false)
}

// ---------------------------------------------------------------------------
// Picker conveniences
// ---------------------------------------------------------------------------

export async function setYolo(yolo: boolean): Promise<void> {
  const conv = get(active)
  if (!conv) return
  active.set({ ...conv, yolo })
  try {
    await api.patchAssistantConversation(conv.id, { yolo })
  } catch (e: any) {
    lastError.set(e?.message || 'Failed to update yolo mode')
  }
}

export async function toggleSaved(): Promise<void> {
  const conv = get(active)
  if (!conv) return
  try {
    if (conv.saved) {
      await api.unsaveAssistantConversation(conv.id)
      active.set({ ...conv, saved: false })
    } else {
      await api.saveAssistantConversation(conv.id)
      active.set({ ...conv, saved: true })
    }
    await refreshConversations()
  } catch (e: any) {
    lastError.set(e?.message || 'Failed to update saved state')
  }
}

export async function deleteConversation(id: string): Promise<void> {
  try {
    await api.deleteAssistantConversation(id)
    if (get(active)?.id === id) active.set(null)
    await refreshConversations()
  } catch (e: any) {
    lastError.set(e?.message || 'Failed to delete conversation')
  }
}

export async function forkConversation(): Promise<void> {
  const conv = get(active)
  if (!conv) return
  try {
    const result: any = await api.forkAssistantConversation(conv.id)
    const forkedId = result?.conversation?.id ?? result?.id
    await refreshConversations()
    if (forkedId) await openConversation(forkedId)
  } catch (e: any) {
    lastError.set(e?.message || 'Failed to fork conversation')
  }
}

export async function exportConversation(fmt: 'json' | 'markdown'): Promise<void> {
  const conv = get(active)
  if (!conv) return
  try {
    await downloadFromApi(
      `/api/assistant/conversations/${conv.id}/export?format=${fmt}`,
      `assistant-${conv.id}.${fmt === 'json' ? 'json' : 'md'}`,
    )
  } catch (e: any) {
    lastError.set(e?.message || 'Export failed')
  }
}
