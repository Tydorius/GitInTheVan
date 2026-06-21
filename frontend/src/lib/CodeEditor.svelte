<script lang="ts">
  import hljs from 'highlight.js/lib/core'
  import javascript from 'highlight.js/lib/languages/javascript'
  import json from 'highlight.js/lib/languages/json'
  import markdown from 'highlight.js/lib/languages/markdown'

  hljs.registerLanguage('javascript', javascript)
  hljs.registerLanguage('json', json)
  hljs.registerLanguage('markdown', markdown)

  export let value: string = ''
  export let language: string = 'javascript'
  export let placeholder: string = ''
  export let minHeight: string = '200px'
  export let errorLine: number | null = null

  let minHeightPx: string = '200px'

  $: {
    const num = parseInt(minHeight)
    minHeightPx = isNaN(num) ? '200px' : (num - 2) + 'px'
  }

  let textarea: HTMLTextAreaElement
  let preEl: HTMLElement
  let highlighted = ''

  function syncScroll() {
    if (preEl && textarea) {
      preEl.scrollTop = textarea.scrollTop
      preEl.scrollLeft = textarea.scrollLeft
    }
  }
  let lineNumbersHtml = ''

  function escapeHtml(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  }

  function updateHighlight() {
    if (!value) {
      highlighted = ''
      lineNumbersHtml = ''
      return
    }

    const lines = value.split('\n')
    const lineCount = lines.length

    let numberedLines: string[] = []
    for (let i = 0; i < lineCount; i++) {
      const lineNum = i + 1
      let escaped = lines[i] || ''

      if (escaped.trim()) {
        try {
          escaped = hljs.highlight(escaped, { language }).value
        } catch {
          escaped = escapeHtml(escaped)
        }
      }

      if (errorLine === lineNum) {
        escaped = `<span class="ce-error-line">${escaped || '&nbsp;'}</span>`
      }

      numberedLines.push(`<span class="ce-code-line">${escaped || '&nbsp;'}</span>`)
    }

    highlighted = numberedLines.join('')

    let nums: string[] = []
    for (let i = 1; i <= lineCount; i++) {
      const cls = errorLine === i ? 'ce-ln-error' : ''
      nums.push(`<span class="${cls}">${i}</span>`)
    }
    lineNumbersHtml = nums.join('\n')
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Tab') {
      e.preventDefault()
      const start = textarea.selectionStart
      const end = textarea.selectionEnd
      value = value.substring(0, start) + '  ' + value.substring(end)
      textarea.selectionStart = textarea.selectionEnd = start + 2
      updateHighlight()
    }
  }

  $: if (value !== undefined) updateHighlight()
  $: if (errorLine !== null) updateHighlight()
</script>

<div class="code-editor-container" style="min-height: {minHeightPx};">
  <div class="code-editor-gutter" aria-hidden="true">{@html lineNumbersHtml || '&nbsp;'}</div>
  <div class="code-editor-main">
    <pre class="code-editor-pre hljs" bind:this={preEl} aria-hidden="true">{@html highlighted || '&nbsp;'}</pre>
    <textarea
      bind:this={textarea}
      bind:value
      onkeydown={handleKeydown}
      oninput={updateHighlight}
      onscroll={syncScroll}
      spellcheck="false"
      autocomplete="off"
      class="code-editor-textarea"
      placeholder={placeholder}
    ></textarea>
  </div>
</div>

<style>
  .code-editor-container {
    display: flex;
    border: 1px solid var(--border, #2e3344);
    border-radius: 8px;
    background: var(--bg, #0f1117);
    overflow: hidden;
  }

  .code-editor-gutter {
    flex-shrink: 0;
    padding: 12px 8px 12px 12px;
    font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 13px;
    line-height: 20px;
    color: var(--text-dim, #8b90a5);
    text-align: right;
    white-space: pre;
    user-select: none;
    background: var(--surface, #1a1d27);
    border-right: 1px solid var(--border, #2e3344);
  }

  .code-editor-main {
    position: relative;
    flex: 1;
    overflow: auto;
  }

  .code-editor-pre {
    margin: 0;
    padding: 12px;
    font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 13px;
    line-height: 20px;
    white-space: pre;
    color: var(--text, #e4e6ef);
    pointer-events: none;
  }

  .code-editor-textarea {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    padding: 12px;
    font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 13px;
    line-height: 20px;
    white-space: pre;
    overflow: auto;
    color: transparent;
    background: transparent;
    caret-color: var(--text, #e4e6ef);
    border: none;
    outline: none;
    resize: none;
    box-sizing: border-box;
  }

  .code-editor-textarea::placeholder {
    color: var(--text-dim, #8b90a5);
    opacity: 0.5;
  }

  :global(.ce-code-line) {
    display: block;
    min-height: 20px;
  }

  :global(.ce-error-line) {
    background: rgba(229, 72, 77, 0.25);
    display: inline;
    border-radius: 2px;
  }

  :global(.ce-ln-error) {
    color: var(--danger, #e5484d);
    font-weight: bold;
  }

  :global(.hljs-keyword) { color: #c678dd; }
  :global(.hljs-string) { color: #98c379; }
  :global(.hljs-number) { color: #d19a66; }
  :global(.hljs-comment) { color: #7f848e; font-style: italic; }
  :global(.hljs-function .hljs-title) { color: #61afef; }
  :global(.hljs-title) { color: #61afef; }
  :global(.hljs-params) { color: var(--text, #e4e6ef); }
  :global(.hljs-built_in) { color: #e6c07b; }
  :global(.hljs-literal) { color: #d19a66; }
  :global(.hljs-attr) { color: #d19a66; }
  :global(.hljs-property) { color: #e06c75; }
  :global(.hljs-variable) { color: #e06c75; }
  :global(.hljs-operator) { color: #56b6c2; }
  :global(.hljs-meta) { color: #7f848e; }
  :global(.hljs-regexp) { color: #98c379; }
  :global(.hljs-symbol) { color: #56b6c2; }

  /* Markdown */
  :global(.hljs-section) { color: #61afef; font-weight: bold; }
  :global(.hljs-bullet) { color: #d19a66; }
  :global(.hljs-quote) { color: #7f848e; font-style: italic; }
  :global(.hljs-emphasis) { color: #c678dd; font-style: italic; }
  :global(.hljs-strong) { color: #c678dd; font-weight: bold; }
  :global(.hljs-link) { color: #61afef; text-decoration: underline; }
  :global(.hljs-link_label) { color: #98c379; }
  :global(.hljs-link_reference) { color: #56b6c2; }
  :global(.hljs-code) { color: #98c379; }
</style>
