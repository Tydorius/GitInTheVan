# Cantrip Context API

The `context` object surface available to cantrip JavaScript, and the sandbox it runs in. Ground truth: `app/services/deno_template.mjs` (the runner shim) and `app/services/deno_runner.py` (the subprocess wrapper).

## JanitorAI-compatible surface

```
context.chat.last_message       - string, the last message
context.chat.last_messages      - array of {message: string} objects
context.chat.message_count      - number of messages
context.chat.conversation_id    - conversation identifier
context.chat.user_name          - the user's name
context.chat.message_created_at - timestamp

context.character.name                - read-only
context.character.personality         - mutable, appendable
context.character.scenario            - mutable, appendable
context.character.example_dialogs     - mutable, appendable
context.character.description         - character description
context.character.first_message       - first message
context.character.alternate_greetings - alternate greetings
```

Cantrips modify `context.character.personality`/`.scenario`/`.example_dialogs` by appending text; the runner returns their final values and the caller injects them.

## GitInTheVan extensions

Per-conversation, per-user, and per-cantrip key/value stores, each with `get(key)`, `set(key, value)`, `keys()`, `delete(key)`:

- `context.chat_data` — keyed by conversation id. Survives across turns of the same chat; isolated per conversation.
- `context.user_data` — keyed by user id only. Shared across every chat and every cantrip the user runs. **Not conversation-scoped** — see `debug-and-sandbox.md` for why this matters during replay/sandbox.
- `context.cantrip_data` — keyed by user id + cantrip id. Persists across chats but is isolated to the one cantrip; also not conversation-scoped.
- `context.memory` — `get`/`set`/`keys`/`delete`/`all()` over the LLM-managed `<memstore>` persistent-memory store for this conversation (distinct from `chat_data`: this is the same store the Driver's response can write to via `<memstore key="...">value</memstore>` tags, injected back as `[PERSISTENT MEMORY]` on later turns).

Other additions:

- `context.budget` — present only when context budgeting is enabled: `{total, remaining, weight, share, detail_level}`, for scaling injected content down under a token ceiling.
- `context.response` — present only for **Pre-Navigator**/**Post-Navigator** position cantrips: `{content, original_content, modified}`. Reassigning `context.response.content` rewrites the Driver's response text.
- `context.tool_call` / `context.tool_result` — present only for **Driver-Callable** invocations: `context.tool_call = {name, args}` (args are the `<call:tool_name arg="value">` attributes, all strings); the cantrip sets `context.tool_result` (a string) to whatever gets returned to the LLM as `[TOOL RESULT]`.
- `console.log(...)` / `console.error(...)` / `console.warn(...)` — all three route to the same debug-log array, visible in the Cantrip Tester's Debug Logs panel and in a Debug run's cantrip metadata. No other console methods exist.
- `"use worker"` — accepted for JanitorAI-script compatibility; cantrip code runs as a plain synchronous function body regardless (`new Function('context', code)`), not an actual Worker.

## Pipeline positions

Four independent checkboxes per cantrip (not mutually exclusive — one cantrip can run at several):

- **Pre-Driver** (default on) — before the Driver call. No `context.response` or `context.tool_call`.
- **Driver-Callable** (opt-in) — invoked mid-generation via `<call:name arg="val">` tags the writing LLM emits; `context.tool_call` is present, `context.response` is not.
- **Pre-Navigator** (opt-in) — after the Driver responds, before verification; `context.response` is present.
- **Post-Navigator** (opt-in) — after verification completes; `context.response` is present.

Branch on `context.response`/`context.tool_call` presence, not on which checkbox you think is active, if the same cantrip runs at more than one position.

## Sandbox restrictions

Cantrip code runs as a Deno subprocess with every permission explicitly denied: `--deny-net`, `--deny-read`, `--deny-write`, `--deny-run`, `--deny-env`, `--deny-ffi`, `--deny-sys`, `--deny-import` (`app/services/deno_runner.py`, `_DENO_DENY_FLAGS`). Deno already denies everything by default when no `--allow-*` flags are granted; the explicit `--deny-*` flags exist so the sandbox's posture is auditable in code rather than implied by omission. Net result: no network, no filesystem, no subprocess execution, no environment variable access, no FFI, no dynamic `import()`.

Each run gets a fresh temp `.mjs` file and a fresh subprocess — no state persists between invocations except through the explicit `chat_data`/`user_data`/`cantrip_data`/`memory` stores.

## Timeouts and size limits

- **Timeout**: per-cantrip, in milliseconds (`Cantrip.timeout_ms`, default 5000), set on the cantrip's edit form. Enforced both as a Python subprocess timeout and reported back as a distinct timeout error if exceeded.
- **Code size**: capped by the admin setting `max_script_size_kb` (default 50 KB), enforced via `check_size()` on every create/update.
- **Content**: cantrip *code* is size-checked and scanned (below) but never run through the control-character-stripping sanitizer applied to other text fields — stripping could corrupt otherwise-valid source.

## Safety scanner (`app/services/safety_scanner.py`)

Every cantrip save runs `scan_cantrip()`, a deterministic regex scan (not an LLM review) with two severities:

- **Critical** — patterns for network access (`fetch(`, `XMLHttpRequest`, `WebSocket`, `Deno.network`/`Deno.connect`), filesystem access (`Deno.readFile`/`writeFile`/`open`/`remove`/`mkdir`), process execution (`Deno.run`, `Deno.Command`), dynamic `import(`, and sandbox-escape attempts (`new Worker()`, `navigator.serviceWorker`). None of these can actually succeed given the deny-flags above; a critical finding means the code is *trying*, not that it *can*.
- **Warning** — dynamic code execution (`eval(`, `Function(`), external URL references, potential infinite loops (`while(true)`, `for(;;)`), and obfuscation indicators: `atob(`, long chained `\x`/`\u` escapes, chained `String.fromCharCode()`, long base64-like blobs, and prototype-pollution-shaped patterns (`__proto__`, `.prototype[`).

Findings are logged to the audit trail (`log_scan_findings`) but **do not block the save** — the caller decides whether to surface a critical finding to the user for override, matching the content-pack install pattern. A critical finding is a signal for a human/reviewing model to look closer, not an enforcement mechanism.

## Minimal correct example

A dice-roll cantrip usable at both Pre-Driver (announces itself) and Driver-Callable (actually rolls on request):

```javascript
if (context.tool_call) {
    // Driver-Callable branch
    const sides = parseInt(context.tool_call.args.sides) || 6;
    const count = parseInt(context.tool_call.args.count) || 1;
    const rolls = Array.from({ length: count }, () =>
        Math.floor(Math.random() * sides) + 1);
    context.tool_result =
        `Rolled ${count}d${sides}: [${rolls.join(', ')}] = ${rolls.reduce((a, b) => a + b, 0)}`;
} else if (context.response) {
    // Pre/Post-Navigator branch: nothing to do here for this cantrip.
} else {
    // Pre-Driver branch
    context.character.scenario += '\nA dice-rolling tool is available this scene.';
    console.log('dice_roller: pre-driver notice added');
}
```

Set **LLM Instructions** to something like: `Roll dice. Args: count (default 1), sides (default 6). Example: <call:dice_roller count="2" sides="20">` — this is what the writing LLM sees in its `[TOOL ACCESS]` notification when the Driver-Callable position is enabled.

## See also

- `#cantrips`
- `#debug`
- `#memories`
- `#content-packs`
