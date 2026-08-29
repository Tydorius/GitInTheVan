/**
 * File download helpers.
 *
 * The same seven-line object-URL dance was copy-pasted into Admin, Lorebooks,
 * Maps and Packs. Centralised here so a fifth caller does not add a fifth copy,
 * and so `revokeObjectURL` is not forgotten in one of them.
 */

import { getToken } from '../api'

/** Trigger a browser download for an in-memory blob. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    link.click()
  } finally {
    // Deferred: Safari has not started reading the blob when click() returns,
    // and revoking synchronously produces an empty file.
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}

/** Serialize a value as pretty-printed JSON and download it. */
export function downloadJson(data: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  downloadBlob(blob, filename)
}

/** Download plain text (Markdown, CSV, logs) under a given media type. */
export function downloadText(text: string, filename: string, type = 'text/plain'): void {
  downloadBlob(new Blob([text], { type: `${type};charset=utf-8` }), filename)
}

/**
 * Fetch an authenticated endpoint that returns a file, and download the result.
 *
 * Goes around `api.request`, which always parses JSON and would corrupt a
 * binary or Markdown body. Honours the server's Content-Disposition filename
 * when it sends one, so the backend stays the single source of naming.
 */
export async function downloadFromApi(
  path: string,
  fallbackFilename: string,
  init: RequestInit = {},
): Promise<void> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init.headers || {}),
      Authorization: `Bearer ${getToken()}`,
    },
  })

  if (!response.ok) {
    let detail = `Download failed (${response.status})`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      // Non-JSON error body; the status line is all we have.
    }
    throw new Error(detail)
  }

  downloadBlob(await response.blob(), filenameFrom(response) || fallbackFilename)
}

/** Read the filename the server asked for, if it sent one. */
function filenameFrom(response: Response): string {
  const header = response.headers.get('content-disposition') || ''
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header)
  return match ? decodeURIComponent(match[1]) : ''
}
