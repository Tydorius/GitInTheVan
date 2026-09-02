/**
 * Minimal Server-Sent-Events client for POST requests.
 *
 * `EventSource` cannot send a POST body or custom headers, so streaming
 * chat/assistant responses go through `fetch` + a manual reader loop instead.
 * Auth handling mirrors `api.ts`'s `request<T>`: same header, same 401
 * behaviour, same FastAPI `detail` unwrapping for error bodies.
 */

import { getToken, clearToken } from '../api'

/** One decoded SSE event: `name` defaults to `'message'` per the spec. */
export type SseHandler = (name: string, data: any) => void

/**
 * POST `body` as JSON to `path` and parse the response as an SSE stream,
 * invoking `onEvent` for each frame. Resolves when the stream ends normally
 * or is aborted via `signal`; rejects on a non-ok response or a read error.
 */
export async function postEventStream(
  path: string,
  body: unknown,
  onEvent: SseHandler,
  signal?: AbortSignal,
): Promise<void> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  let resp: Response
  try {
    resp = await fetch(path, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal,
    })
  } catch (err) {
    if ((err as any)?.name === 'AbortError') return
    throw err
  }

  if (resp.status === 401) {
    clearToken()
    window.location.hash = '#/login'
    throw new Error('Unauthorized')
  }

  if (!resp.ok) {
    const text = await resp.text()
    let data: unknown
    try {
      data = text ? JSON.parse(text) : null
    } catch {
      data = text
    }
    const detail = (data as any)?.detail
    let msg: string
    if (typeof detail === 'string') {
      msg = detail
    } else if (Array.isArray(detail)) {
      msg = detail.map((e: any) => `${e.loc?.join('.') || e.msg}: ${e.msg || ''}`).join('; ')
    } else if (typeof detail === 'object' && detail) {
      msg = JSON.stringify(detail)
    } else {
      msg = resp.statusText
    }
    throw new Error(msg || resp.statusText)
  }

  if (!resp.body) return

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  const abortHandler = () => {
    reader.cancel().catch(() => {})
  }
  signal?.addEventListener('abort', abortHandler)

  try {
    while (true) {
      let result: ReadableStreamReadResult<Uint8Array>
      try {
        result = await reader.read()
      } catch (err) {
        if (signal?.aborted || (err as any)?.name === 'AbortError') return
        throw err
      }
      if (result.done) break

      buffer += decoder.decode(result.value, { stream: true })

      // Frames are separated by a blank line; tolerate CRLF line endings.
      let boundary: { start: number; end: number } | null
      while ((boundary = findFrameBoundary(buffer)) !== null) {
        const frame = buffer.slice(0, boundary.start)
        buffer = buffer.slice(boundary.end)
        dispatchFrame(frame, onEvent)
      }
    }

    // Flush a trailing frame with no closing blank line at EOF.
    buffer += decoder.decode()
    if (buffer.trim()) {
      dispatchFrame(buffer, onEvent)
    }
  } finally {
    signal?.removeEventListener('abort', abortHandler)
    try {
      reader.releaseLock()
    } catch {
      // Already released (e.g. after cancel()).
    }
  }
}

/**
 * Find the next blank-line frame boundary in `buffer`. Returns the frame's
 * text range `{ start, end }` (end is past the blank line, i.e. where the
 * next frame begins), or null if no complete frame is buffered yet.
 */
function findFrameBoundary(buffer: string): { start: number; end: number } | null {
  const lf = buffer.indexOf('\n\n')
  const crlf = buffer.indexOf('\r\n\r\n')

  if (lf === -1 && crlf === -1) return null

  if (crlf !== -1 && (lf === -1 || crlf <= lf)) {
    return { start: crlf, end: crlf + 4 }
  }
  return { start: lf, end: lf + 2 }
}

/** Parse one SSE frame's `event:`/`data:` lines and invoke the handler. */
function dispatchFrame(frame: string, onEvent: SseHandler): void {
  const lines = frame.split(/\r\n|\n/)
  let eventName = ''
  const dataLines: string[] = []

  for (const line of lines) {
    if (line === '' || line.startsWith(':')) continue // comment / blank
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
    // id:/retry: intentionally ignored.
  }

  if (dataLines.length === 0) return

  const raw = dataLines.join('\n')
  let parsed: any
  try {
    parsed = JSON.parse(raw)
  } catch {
    parsed = raw
  }
  onEvent(eventName || 'message', parsed)
}
