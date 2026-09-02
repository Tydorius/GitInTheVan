/**
 * Line-level diff for showing the assistant's proposed edits.
 *
 * Uses a classic O(m*n) LCS table over the two files' lines, then backtracks
 * to produce a same/add/del sequence. Good enough for the sizes this side
 * pane actually shows (a single file's contents); guarded by MAX_LINES so a
 * pathological huge paste can't blow up memory or time.
 */

export type DiffLine = { kind: 'same' | 'add' | 'del'; text: string }

/** Above this many lines on either side, skip the LCS and show a flat diff. */
const MAX_LINES = 2000

/** Split on '\n' without producing a phantom empty line for a trailing '\n'. */
function splitLines(src: string): string[] {
  if (src === '') return []
  const lines = src.split('\n')
  if (src.endsWith('\n')) {
    lines.pop()
  }
  return lines
}

/**
 * Compute a line-level diff of `a` (old) against `b` (new).
 *
 * If either side exceeds MAX_LINES lines, skips the O(m*n) LCS table
 * (which would be too large/slow) and returns all of `a` as deletions
 * followed by all of `b` as additions.
 */
export function lineDiff(a: string, b: string): DiffLine[] {
  const linesA = splitLines(a)
  const linesB = splitLines(b)

  if (linesA.length > MAX_LINES || linesB.length > MAX_LINES) {
    const result: DiffLine[] = []
    for (const text of linesA) result.push({ kind: 'del', text })
    for (const text of linesB) result.push({ kind: 'add', text })
    return result
  }

  const m = linesA.length
  const n = linesB.length

  // dp[i][j] = length of the LCS of linesA[i..] and linesB[j..].
  const dp: Uint32Array[] = new Array(m + 1)
  for (let i = 0; i <= m; i++) dp[i] = new Uint32Array(n + 1)

  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      if (linesA[i] === linesB[j]) {
        dp[i][j] = dp[i + 1][j + 1] + 1
      } else {
        dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1])
      }
    }
  }

  const result: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < m && j < n) {
    if (linesA[i] === linesB[j]) {
      result.push({ kind: 'same', text: linesA[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      result.push({ kind: 'del', text: linesA[i] })
      i++
    } else {
      result.push({ kind: 'add', text: linesB[j] })
      j++
    }
  }
  while (i < m) {
    result.push({ kind: 'del', text: linesA[i] })
    i++
  }
  while (j < n) {
    result.push({ kind: 'add', text: linesB[j] })
    j++
  }

  return result
}

/** Count added/removed lines in a diff produced by lineDiff. */
export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const line of lines) {
    if (line.kind === 'add') added++
    else if (line.kind === 'del') removed++
  }
  return { added, removed }
}

// Verified by hand:
//
// 1. lineDiff('a\nb\nc', 'a\nx\nc')
//    => [
//         { kind: 'same', text: 'a' },
//         { kind: 'del',  text: 'b' },
//         { kind: 'add',  text: 'x' },
//         { kind: 'same', text: 'c' },
//       ]
//    diffStats(...) => { added: 1, removed: 1 }
//
// 2. lineDiff('line1\nline2', 'line1\nline2')
//    => [
//         { kind: 'same', text: 'line1' },
//         { kind: 'same', text: 'line2' },
//       ]
//    diffStats(...) => { added: 0, removed: 0 }
//
// 3. lineDiff('', 'hello')
//    => [{ kind: 'add', text: 'hello' }]
//    diffStats(...) => { added: 1, removed: 0 }
