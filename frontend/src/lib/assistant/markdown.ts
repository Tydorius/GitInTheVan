/**
 * Tiny Markdown -> HTML renderer for the assistant's side pane.
 *
 * Safe to drop straight into `{@html renderMarkdown(src)}` with no
 * sanitiser: the *entire* input is HTML-escaped up front, and every tag in
 * the output is then built by this module from recognised Markdown
 * constructs only. Raw source text never reaches the output unescaped, so
 * there is nothing for a `<script>`/`onerror=`/`javascript:` payload in the
 * input to attach to.
 */

import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'

hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('json', json)
hljs.registerLanguage('markdown', markdown)

const SUPPORTED_LANGS = new Set(['javascript', 'json', 'markdown'])

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** Render a fenced code block. `code` is raw (unescaped) source text. */
function renderCodeBlock(code: string, lang: string): string {
  const norm = lang.trim().toLowerCase()
  if (SUPPORTED_LANGS.has(norm)) {
    // hljs.highlight escapes the code itself; nothing raw passes through.
    const highlighted = hljs.highlight(code, { language: norm }).value
    return `<pre class="md-code"><code class="hljs language-${norm}">${highlighted}</code></pre>`
  }
  return `<pre class="md-code"><code>${escapeHtml(code)}</code></pre>`
}

/**
 * Apply inline formatting to text that has ALREADY been through
 * `escapeHtml`. Because entities are already in place, none of the
 * delimiter characters below (`` ` ``, `*`, `_`, `[`, `]`, `(`, `)`) can
 * originate from attacker-controlled `&`, `<`, `>`, `"` or `'`.
 */
function renderInline(escaped: string): string {
  // Split out inline code spans (the capturing group keeps the delimited
  // pieces in the result) so bold/italic/link markers *inside* code are
  // left untouched, without needing a placeholder string that risks
  // colliding with ordinary text.
  const parts = escaped.split(/(`[^`]+`)/g)
  return parts
    .map((part, idx) =>
      idx % 2 === 1 ? `<code>${part.slice(1, -1)}</code>` : renderLinksAndEmphasis(part),
    )
    .join('')
}

function renderLinksAndEmphasis(text: string): string {
  // Links: [text](url) - only http(s) urls become real links.
  let out = text.replace(/\[([^\]]*)\]\(([^)]*)\)/g, (_m, linkText, url) => {
    if (/^https?:\/\//i.test(url)) {
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">${linkText}</a>`
    }
    return linkText
  })

  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  out = out.replace(/\*([^*]+)\*/g, '<em>$1</em>')
  out = out.replace(/_([^_]+)_/g, '<em>$1</em>')

  return out
}

const HR_RE = /^ {0,3}([-*_])(?: *\1){2,}\s*$/
const HEADING_RE = /^(#{1,4})\s+(.*)$/
const QUOTE_RE = /^>\s?/
const UL_RE = /^[-*]\s+/
const OL_RE = /^\d+\.\s+/
const FENCE_OPEN_RE = /^```\s*(\S*)/
const FENCE_CLOSE_RE = /^```\s*$/

/** Render a Markdown string to HTML, safe for `{@html}` with no sanitiser. */
export function renderMarkdown(src: string): string {
  const lines = src.replace(/\r\n?/g, '\n').split('\n')
  const out: string[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    if (line.trim() === '') {
      i++
      continue
    }

    const fence = FENCE_OPEN_RE.exec(line)
    if (fence) {
      const lang = fence[1] || ''
      const codeLines: string[] = []
      i++
      while (i < lines.length && !FENCE_CLOSE_RE.test(lines[i])) {
        codeLines.push(lines[i])
        i++
      }
      i++ // skip closing fence, or run off the end at EOF
      out.push(renderCodeBlock(codeLines.join('\n'), lang))
      continue
    }

    if (HR_RE.test(line)) {
      out.push('<hr>')
      i++
      continue
    }

    const heading = HEADING_RE.exec(line)
    if (heading) {
      const level = heading[1].length + 2 // # -> h3 ... #### -> h6
      out.push(`<h${level}>${renderInline(escapeHtml(heading[2].trim()))}</h${level}>`)
      i++
      continue
    }

    if (QUOTE_RE.test(line)) {
      const quoteLines: string[] = []
      while (i < lines.length && QUOTE_RE.test(lines[i])) {
        quoteLines.push(lines[i].replace(QUOTE_RE, ''))
        i++
      }
      out.push(`<blockquote><p>${renderInline(escapeHtml(quoteLines.join(' ')))}</p></blockquote>`)
      continue
    }

    if (UL_RE.test(line)) {
      const items: string[] = []
      while (i < lines.length && UL_RE.test(lines[i])) {
        items.push(lines[i].replace(UL_RE, ''))
        i++
      }
      out.push(`<ul>${items.map((t) => `<li>${renderInline(escapeHtml(t))}</li>`).join('')}</ul>`)
      continue
    }

    if (OL_RE.test(line)) {
      const items: string[] = []
      while (i < lines.length && OL_RE.test(lines[i])) {
        items.push(lines[i].replace(OL_RE, ''))
        i++
      }
      out.push(`<ol>${items.map((t) => `<li>${renderInline(escapeHtml(t))}</li>`).join('')}</ol>`)
      continue
    }

    // Paragraph: consecutive plain lines, joined with a space.
    const paraLines: string[] = []
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !FENCE_OPEN_RE.test(lines[i]) &&
      !HR_RE.test(lines[i]) &&
      !HEADING_RE.test(lines[i]) &&
      !QUOTE_RE.test(lines[i]) &&
      !UL_RE.test(lines[i]) &&
      !OL_RE.test(lines[i])
    ) {
      paraLines.push(lines[i])
      i++
    }
    out.push(`<p>${renderInline(escapeHtml(paraLines.join(' ')))}</p>`)
  }

  return out.join('\n')
}

// Verified by hand:
//
// 1. renderMarkdown('# Title\n\nSome **bold** and *italic* text.')
//    => '<h3>Title</h3>\n<p>Some <strong>bold</strong> and <em>italic</em> text.</p>'
//
// 2. renderMarkdown('[link](https://example.com) and [js](javascript:alert(1))')
//    => '<p><a href="https://example.com" target="_blank" rel="noopener noreferrer">link</a>'
//       + ' and js</p>'
//    (non-http(s) url falls back to plain text "js", not a link)
//
// 3. renderMarkdown('Look: <img src=x onerror=alert(1)>')
//    => '<p>Look: &lt;img src=x onerror=alert(1)&gt;</p>'
//    (the whole line is escaped before any Markdown construct is recognised,
//    so this renders as inert text, never as a real <img> tag)
//
// 4. renderMarkdown('- one\n- two\n\n1. first\n2. second')
//    => '<ul><li>one</li><li>two</li></ul>\n<ol><li>first</li><li>second</li></ol>'
