const __debugLogs = [];
console.log = (...args) => {
    __debugLogs.push(args.map(a => {
        if (typeof a === 'object') {
            try { return JSON.stringify(a); } catch { return String(a); }
        }
        return String(a);
    }).join(' '));
};
console.error = console.warn = console.log;

// Base64 payloads are UTF-8 bytes. atob() alone yields a Latin-1 "binary
// string" (one char per byte), which mojibakes every multi-byte character --
// curly quotes and em-dashes in cantrip code, accents and emoji in stored
// data. Decode the byte string through TextDecoder instead.
const __b64 = (s) => new TextDecoder().decode(Uint8Array.from(atob(s), c => c.charCodeAt(0)));

const context = JSON.parse(__b64("__GITV_CONTEXT__"));
const __chatData = JSON.parse(__b64("__GITV_CHATDATA__"));
const __userData = JSON.parse(__b64("__GITV_USERDATA__"));
const __cantripData = JSON.parse(__b64("__GITV_CANTRIPDATA__"));
const __userCode = __b64("__GITV_USERCODE__");

context.chat_data = {
    get: (key) => key in __chatData ? __chatData[key] : null,
    set: (key, value) => { __chatData[key] = value; },
    keys: () => Object.keys(__chatData),
    delete: (key) => { delete __chatData[key]; }
};

context.user_data = {
    get: (key) => key in __userData ? __userData[key] : null,
    set: (key, value) => { __userData[key] = value; },
    keys: () => Object.keys(__userData),
    delete: (key) => { delete __userData[key]; }
};

context.cantrip_data = {
    get: (key) => key in __cantripData ? __cantripData[key] : null,
    set: (key, value) => { __cantripData[key] = value; },
    keys: () => Object.keys(__cantripData),
    delete: (key) => { delete __cantripData[key]; }
};

const __memories = context.__memories || {};
delete context.__memories;
context.memory = {
    get: (key) => key in __memories ? __memories[key] : null,
    set: (key, value) => { __memories[key] = String(value); },
    keys: () => Object.keys(__memories),
    delete: (key) => { delete __memories[key]; },
    all: () => Object.assign({}, __memories)
};

// Map nominations (0.33.0): a script can ask for a map by tag. Collection
// only -- the host treats it as a selection candidate under the Activation
// Hierarchy, never a guaranteed run.
const __mapNominations = [];
context.maps = {
    activate: (tag) => {
        if (typeof tag === 'string' && tag) __mapNominations.push(tag);
    }
};

// Passenger (Phase 17): the sanctioned LLM egress. The host injects either
// null (feature off or no handler: enabled=false, call() resolves null with
// zero protocol traffic) or {"enabled":true,"maxCalls":N}. Requests go out
// one JSON line per call on stderr behind the __GITV_MSG__ sentinel (so Deno
// diagnostics can never be mistaken for protocol traffic); replies arrive as
// JSON lines on stdin, matched by id. The host-side budget counter is
// authoritative -- the local one only answers over-cap calls without a round
// trip.
const __passengerCfg = __GITV_PASSENGER__;
const __passengerPending = new Map();
let __passengerSeq = 0;
let __passengerLocalUsed = 0;
const __GITV_MSG_PREFIX = "__GITV_MSG__ ";

function __passengerCall(params) {
    return new Promise((resolve) => {
        if (!__passengerCfg || !__passengerCfg.enabled) {
            __debugLogs.push('passenger: call refused, passenger is disabled');
            resolve(null);
            return;
        }
        if (typeof params !== 'object' || params === null) {
            __debugLogs.push('passenger: call refused, params must be an object');
            resolve(null);
            return;
        }
        const tag = typeof params.tag === 'string' ? params.tag : '';
        const instructions = typeof params.instructions === 'string' ? params.instructions : '';
        const ctx = typeof params.context === 'string' ? params.context : '';
        const model = typeof params.model === 'string' ? params.model : '';
        if (!instructions.trim()) {
            __debugLogs.push('passenger: call refused, instructions must be a non-empty string');
            resolve(null);
            return;
        }
        if (__passengerLocalUsed >= __passengerCfg.maxCalls) {
            __debugLogs.push('passenger: call refused, per-request call cap reached');
            resolve(null);
            return;
        }
        __passengerLocalUsed += 1;
        __passengerSeq += 1;
        const id = __passengerSeq;
        __passengerPending.set(id, resolve);
        try {
            const line = JSON.stringify({
                type: 'passenger_call', id, tag, instructions, context: ctx, model
            });
            Deno.stderr.writeSync(new TextEncoder().encode(__GITV_MSG_PREFIX + line + "\n"));
        } catch (e) {
            __passengerPending.delete(id);
            __debugLogs.push('passenger: call failed, ' + (e.message || String(e)));
            resolve(null);
        }
    });
}

// Started before the user code so replies can arrive while it runs. ONLY
// when Passenger is enabled: an open stdin read keeps Deno's event loop
// alive, so an unconditional reader would hang every plain cantrip run to
// its timeout on the non-interactive path where stdin is inherited. On
// stdin EOF (process exiting) every still-pending call resolves null, so
// the final result can always serialize. Unknown ids and malformed lines
// are ignored with a debug-log note, never thrown.
let __passengerStdinLoop = null;
if (__passengerCfg && __passengerCfg.enabled) {
    __passengerStdinLoop = (async () => {
        const decoder = new TextDecoder();
        let buffer = "";
        try {
            for await (const chunk of Deno.stdin.readable) {
                buffer += decoder.decode(chunk, { stream: true });
                let idx;
                while ((idx = buffer.indexOf("\n")) !== -1) {
                    const line = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 1);
                    if (!line.trim()) continue;
                    let msg = null;
                    try { msg = JSON.parse(line); } catch { /* noted below */ }
                    if (msg && msg.type === 'passenger_result' && typeof msg.id === 'number') {
                        const resolveFn = __passengerPending.get(msg.id);
                        if (resolveFn) {
                            __passengerPending.delete(msg.id);
                            resolveFn(msg.ok === true && typeof msg.text === 'string' ? msg.text : null);
                        } else {
                            __debugLogs.push('passenger: stdin reply for unknown id ' + msg.id);
                        }
                    } else {
                        __debugLogs.push('passenger: ignored malformed stdin line');
                    }
                }
            }
        } catch { /* stdin closed underneath us; resolve pending below */ }
        for (const resolveFn of __passengerPending.values()) resolveFn(null);
        __passengerPending.clear();
    })();
}

context.passenger = {
    enabled: !!(__passengerCfg && __passengerCfg.enabled),
    call: __passengerCall
};

if (!context.character) context.character = {};
if (!context.character.personality) context.character.personality = "";
if (!context.character.scenario) context.character.scenario = "";
if (!context.character.example_dialogs) context.character.example_dialogs = "";

let __hasResponse = false;
if (context.response) {
    __hasResponse = true;
    if (typeof context.response.content !== 'string') context.response.content = "";
    if (typeof context.response.original_content !== 'string') context.response.original_content = context.response.content;
    if (typeof context.response.modified !== 'boolean') context.response.modified = false;
}

let __hasToolCall = false;
if (context.tool_call) {
    __hasToolCall = true;
}
if (typeof context.tool_result !== 'string') context.tool_result = "";

let __error = null;
try {
    // The async wrapper makes top-level `await` work (Passenger calls are
    // Promises); purely synchronous scripts are unaffected. The trim fallback
    // tolerates user code ending in a stray closing brace, as before.
    let fn;
    try {
        fn = new Function('context', 'return (async () => {\n' + __userCode + '\n})();');
    } catch (e1) {
        const trimmed = __userCode.replace(/\n\s*\}\s*$/, '');
        fn = new Function('context', 'return (async () => {\n' + trimmed + '\n})();');
    }
    await fn(context);
} catch (e) {
    __error = e.message || String(e);
    __debugLogs.push('SCRIPT ERROR: ' + __error);
}

// User code done: close stdin so the reply reader EOFs and the process can
// exit -- an open stdin read keeps Deno's event loop alive. Calls the script
// never awaited resolve null (their replies cannot arrive after this); a
// script that wants a result must await it.
if (__passengerStdinLoop) {
    try { Deno.stdin.close(); } catch { /* already closed */ }
    try { await __passengerStdinLoop; } catch { /* reader aborted */ }
}

const __result = {
    personality: context.character.personality || "",
    scenario: context.character.scenario || "",
    example_dialogs: context.character.example_dialogs || "",
    response_content: __hasResponse ? (context.response.content || null) : null,
    tool_result: context.tool_result || "",
    chat_data: Object.assign({}, __chatData),
    user_data: Object.assign({}, __userData),
    cantrip_data: Object.assign({}, __cantripData),
    memories: Object.assign({}, __memories),
    maps_nominated: __mapNominations,
    debug_logs: __debugLogs,
    error: __error
};

Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify(__result)));
