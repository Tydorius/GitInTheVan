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

context.character.name                - derived from the request when present
context.character.personality         - mutable (append or replace)
context.character.scenario            - mutable (append or replace)
context.character.example_dialogs     - mutable (append or replace)
context.character.description         - character description
context.character.first_message       - first message
context.character.alternate_greetings - alternate greetings
```

On JanitorAI-shaped requests (the system message carries the card's tagged sections — `<Char's Persona>`, `<Scenario>`, `<example_dialogs>`), the three mutable fields are **seeded with the card's real values** and `character.name` is derived from the persona marker. The runner returns each field's final value and the caller splices it back into the card's own section, so assignment (`=`) replaces the card text and `+=` appends to it — JanitorAI semantics. Multiple cantrips run sequentially: each sees the previous cantrip's field output, and the final value wins.

Requests without those sections (non-JanitorAI clients) keep the historical behavior: fields start empty and cantrip output is injected as separate `[Personality]`/`[Scenario]`/`[Example Dialogs]` system-message blocks alongside the untouched original prompt.

Not recoverable from a proxied request: `description` (JanitorAI never sends it to the LLM), `first_message` and `alternate_greetings` (they exist as greeting messages, not card sections). These stay empty in live runs; the Cantrip Tester's explicit inputs still populate them.

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
- `context.passenger` — scripted side LLM calls (0.36.0): `{enabled: boolean, call(params): Promise<string | null>}`. `call({tag, instructions, context, model})` makes one LLM call through the host — the script never touches a URL, header or key. `instructions` becomes the system message, `context` the user message, `tag` picks the endpoint role tag (empty/unmatched falls back to the user's default chain), `model` defaults to the endpoint's. Resolves to the generated text or `null` on any failure (disabled, over the per-request cap, oversized payload, all endpoints failed, timeout, empty output); the request always forwards untouched. Every call costs from one per-request budget shared by all cantrips and map stages (min of the admin cap and the user's own budget). Branch on `context.passenger.enabled` for a script-side fallback; the host never injects fallback text. `await` works at top level. Ground truth: `app/services/passenger.py`.
- `console.log(...)` / `console.error(...)` / `console.warn(...)` — all three route to the same debug-log array, visible in the Cantrip Tester's Debug Logs panel and in a Debug run's cantrip metadata. No other console methods exist.
- `"use worker"` — accepted for JanitorAI-script compatibility; cantrip code runs wrapped in an async function body (`new Function('context', 'return (async () => {...})()')`) so top-level `await` works (Passenger calls are Promises), not an actual Worker.

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

## Response tags, activate(), inject() (0.39.0)

- `context.response_tags` — object of `{tagName: [payload, ...]}` seeded into post-Navigator scripts the same turn the model wrote them. The cantrip registers tag names (Response Tags field); GitV extracts them from the final reply, strips them before the client, and delivers here. Unregistered tags pass through untouched.
- `context.activate(name)` — turn one of the user's existing lorebooks on for this request only (union source; see `#activation-hierarchy`).
- `context.inject(text, position)` — place prompt text at `system_start`, `before_last_message`, `in_chat_system` (a system message at the very end), or `user_tail` (appended to the newest user message; multimodal-safe; falls back to appending a new user message if none exists). Max 20 per request.
- `context.request.id` — stable id for the current request.
- `context.request.is_regenerate` — true when this request repeats the previous one's shape (same message count, same trailing user message): a swipe regeneration.
- `context.chat.last_messages` entries now carry `role` alongside `message`.

## History control (`context.history`, 0.46.0)

Pre-Driver cantrips can trim or drop messages from the forwarded prompt without affecting conversation identity hashing:

- `context.history.keep_recent(n)` — preserves all `system` messages in their original relative order and keeps the newest `n` non-system messages. The newest `user` message is always preserved even when `n = 0`.
- `context.history.drop(indices)` — drops messages at the specified integer indices (accepts a single integer or an array; supports negative Python-style indices such as `-2`). The newest `user` message is always protected and silently skipped if listed.

Operations are collected in order (max 20 per request; a single `drop` carries at most 200 indices) and applied host-side only to the forwarded `messages` list; the conversation-hash anchor is never mutated.

Indices resolve against the list **as mutated by earlier operations** — a `keep_recent` earlier in the same request (from this cantrip or a previous one) shifts every index after it. Compute `drop` indices from the trimmed view, or drop first and trim second.

## See also

- `#cantrips`
- `#debug`
- `#memories`
- `#content-packs`
